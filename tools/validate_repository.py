"""Validate the distribution offline; never queries APIs or models."""
from pathlib import Path
import ast
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate(root=ROOT):
    root = Path(root).resolve()
    plugin = root / "plugins/offerist"
    manifests = [json.loads((plugin / p).read_text(encoding="utf-8")) for p in ("plugin.json", ".codex-plugin/plugin.json")]
    assert manifests[0]["name"] == manifests[1]["name"] == "offerist"
    assert manifests[0]["version"] == manifests[1]["version"]
    catalog = json.loads((root / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    assert catalog["name"] == "seomeat-offerist"
    assert catalog["plugins"][0]["source"]["path"] == "./plugins/offerist"
    skills = sorted((plugin / "skills").glob("*/SKILL.md"))
    assert len(skills) == 25
    skill_catalog = (root / "docs/skills.md").read_text(encoding="utf-8")
    for path in skills:
        body = path.read_text(encoding="utf-8")
        front = re.match(r"^---\s*\n(.*?)\n---\s*\n", body, re.S)
        assert front, path
        meta = yaml.safe_load(front.group(1))
        assert meta["name"] == path.parent.name and meta["description"].strip()
        assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
        assert meta["name"] in skill_catalog
        ui = path.parent / "agents/openai.yaml"
        if ui.exists():
            interface = yaml.safe_load(ui.read_text(encoding="utf-8"))["interface"]
            assert 25 <= len(interface["short_description"]) <= 64, path
            assert "$" + meta["name"] in interface["default_prompt"], path
    files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    links = 0
    for path in files:
        assert not path.is_symlink() and path.resolve().is_relative_to(root)
        relative = path.relative_to(root)
        assert not any(p in {".offerist", "private", "client-data", "local-runs"} for p in relative.parts), relative
        assert not path.name.startswith(".env") and not re.search(r"(?:token|credentials).*\.json$", path.name, re.I), relative
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        if path.suffix in {".md", ".json", ".yaml", ".yml", ".py", ".mjs", ".csv"}:
            body = path.read_text(encoding="utf-8")
            assert not re.search(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", body), relative
            assert not re.search(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", body), relative
        if path.suffix == ".md":
            for raw in re.findall(r"\]\(([^)]+)\)", body):
                link = raw.strip("<>")
                if re.match(r"[a-zA-Z]+:", link) or link.startswith("#"):
                    continue
                target = (path.parent / link.split("#")[0]).resolve()
                assert target.is_relative_to(root) and target.exists(), (relative, link)
                links += 1
    sample = root / "examples/demo/proposal.docx"
    reference = plugin / "skills/offerist-quick/assets/reference-proposal.docx"
    assert hashlib.sha256(sample.read_bytes()).digest() == hashlib.sha256(reference.read_bytes()).digest()
    with zipfile.ZipFile(sample) as z:
        assert z.testzip() is None
        assert not any("embeddings/" in n or n.endswith("vbaProject.bin") for n in z.namelist())
        for name in z.namelist():
            if name.endswith(".xml"):
                text = z.read(name).decode("utf-8")
                assert "wood" + "cort" not in text.lower()
                assert not re.search(r"[A-Za-z]:[\\/]+Users[\\/]+", text)
        doc = ET.fromstring(z.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"}
        counts = {"tables": len(doc.findall(".//w:tbl", ns)), "inline_drawings": len(doc.findall(".//wp:inline", ns))}
        assert counts["tables"] >= 8 and counts["inline_drawings"] == 2
    assert (root / "examples/demo/proposal.pdf").read_bytes().startswith(b"%PDF-")
    return {"version": manifests[0]["version"], "skills": len(skills), "relative_links_checked": links,
            "files_checked": len(files), "sample": counts, "actual_client_reference_excluded": True,
            "semantic_acceptance": "not_claimed"}


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False, indent=2))
