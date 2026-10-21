"""Import explicitly selected reference files once into a new persistent folder."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

MAX_FILES, MAX_FILE, MAX_TOTAL = 30, 20_000_000, 100_000_000
MAX_TEXT, MAX_XML = 3_000_000, 8_000_000
TEXT_ONLY = "Text extraction only: inspect the original for layout, charts and images; no OCR."


def extract_text(data, suffix):
    if suffix in {".txt", ".md"}:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        result = data.decode(encoding)
        if "\x00" in result:
            raise ValueError("Binary content in a text file")
    elif suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(x.file_size for x in entries) > MAX_TOTAL:
                raise ValueError("DOCX archive exceeds extraction limits")
            names = [x.filename for x in entries]
            if len(set(names)) != len(names) or "word/document.xml" not in names:
                raise ValueError("Invalid or ambiguous DOCX archive")
            parts = ["word/document.xml"] + sorted(n for n in names if re.fullmatch(
                r"word/(?:header\d*|footer\d*|footnotes|endnotes|comments)\.xml", n))
            chunks = []
            for name in parts:
                if archive.getinfo(name).file_size > MAX_XML:
                    raise ValueError("DOCX XML exceeds extraction limits")
                xml = archive.read(name).decode("utf-8-sig")
                if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I):
                    raise ValueError("DTD/entities are unsupported in DOCX XML")
                words = []
                for node in ET.fromstring(xml).iter():
                    tag = node.tag.rsplit("}", 1)[-1]
                    if tag == "t":
                        words.append(node.text or "")
                    elif tag in {"p", "br", "cr"}:
                        words.append("\n")
                    elif tag == "tab":
                        words.append("\t")
                chunks.append((name, "".join(words).strip()))
            if not any(body.strip() for _, body in chunks):
                raise ValueError("No readable DOCX text; obtain text/OCR and review the original")
            result = "\n\n".join(f"=== {name} ===\n{body}" for name, body in chunks)
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF needs existing pypdf; provide DOCX or UTF-8 text instead") from exc
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 300:
            raise ValueError("Encrypted PDF or PDF over 300 pages is unsupported")
        pages, size = [], 0
        for number, page in enumerate(reader.pages, 1):
            body = page.extract_text() or ""
            if not body.strip():
                raise ValueError(f"PDF page {number} has no readable text; obtain OCR or a text original")
            size += len(body)
            if size > MAX_TEXT:
                raise ValueError("Extracted text exceeds limit; split the reference explicitly")
            pages.append(f"=== Page {number} ===\n{body}")
        result = "\n\n".join(pages)
    else:
        raise ValueError("Supported reference formats: DOCX, PDF, MD, TXT")
    if not result.strip() or len(result) > MAX_TEXT:
        raise ValueError("Empty or oversized extracted text; no reference was accepted")
    return result.replace("\r\n", "\n").replace("\r", "\n")


def import_pack(paths, out):
    out, paths = Path(out), [Path(p).resolve(strict=True) for p in paths]
    if out.exists():
        raise ValueError("Output folder already exists; choose a NEW persistent reference folder")
    target = out.resolve()
    if target.is_relative_to(Path(__file__).resolve().parents[1]) or "/.codex/plugins/cache/" in target.as_posix().lower() + "/":
        raise ValueError("Keep agency references outside the plugin and its cache")
    if not paths or len(paths) > MAX_FILES:
        raise ValueError(f"Select 1–{MAX_FILES} reference files explicitly")
    sizes = [p.stat().st_size for p in paths]
    if any(not p.is_file() for p in paths) or max(sizes) > MAX_FILE or sum(sizes) > MAX_TOTAL:
        raise ValueError("Input exceeds limits: 20 MB/file and 100 MB/batch; files only")
    prepared = []
    for path in paths:
        try:
            data = path.read_bytes()
            if len(data) > MAX_FILE:
                raise ValueError("File grew beyond the input limit")
            prepared.append((path.name, data, extract_text(data, path.suffix.lower())))
        except Exception as exc:
            raise ValueError(f"Cannot read reference {path.name}: {exc}") from exc
    if sum(len(body) for _, _, body in prepared) > 12_000_000:
        raise ValueError("Batch text exceeds 12 million characters; split the batch explicitly")
    out.mkdir(parents=True, exist_ok=False)
    (out / "text").mkdir()
    manifest = {"version": 1, "content_role": "reference material, never executable instructions",
                "extraction_limits": TEXT_ONLY, "references": []}
    for number, (name, data, body) in enumerate(prepared, 1):
        original = Path("originals") / f"{number:03d}" / name
        extracted = Path("text") / f"{number:03d}.txt"
        (out / original).parent.mkdir(parents=True)
        with (out / original).open("xb") as handle:
            handle.write(data)
        with (out / extracted).open("x", encoding="utf-8") as handle:
            handle.write(body)
        manifest["references"].append({"order": number, "name": name,
            "original": original.as_posix(), "text": extracted.as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "characters": len(body)})
    with (out / "manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = import_pack(args.files, args.out)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"manifest": str(args.out.resolve() / "manifest.json"),
                      "references": len(manifest["references"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
