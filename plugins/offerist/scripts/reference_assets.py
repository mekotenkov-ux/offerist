"""Index selected proposal references offline: figures, page previews and source links.

Never follows links. Original files and an existing output directory are untouched.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

MAX_FILE, MAX_TOTAL, MAX_ASSETS = 20_000_000, 100_000_000, 500


def safe_link(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def inspect_docx(data):
    from PIL import Image
    figures, links = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(x.file_size for x in entries) > MAX_TOTAL:
            raise ValueError('DOCX exceeds extraction limits')
        names = [x.filename for x in entries]
        if len(names) != len(set(names)):
            raise ValueError('Ambiguous DOCX archive')
        for name in names:
            if re.fullmatch(r'word/media/[^/]+', name) and Path(name).suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                content = archive.read(name)
                if len(content) > MAX_FILE:
                    raise ValueError('Embedded image exceeds limit')
                with Image.open(io.BytesIO(content)) as image:
                    if image.format not in {'PNG', 'JPEG'} or image.width * image.height > 25_000_000:
                        raise ValueError('Unsupported or oversized image')
                    width, height = image.size
                    image.verify()
                figures.append({'kind': 'embedded_image', 'source_part': name,
                                'width': width, 'height': height, 'data': content,
                                'suffix': Path(name).suffix.lower()})
        for name in names:
            if re.fullmatch(r'word/(?:_rels/document.xml.rels|document.xml)', name):
                content = archive.read(name)
                if len(content) > 8_000_000 or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', content, re.I):
                    raise ValueError('Oversized XML or DTD/entities')
        relpath = 'word/_rels/document.xml.rels'
        if relpath in names:
            relations = {r.attrib['Id']: r.attrib for r in ET.fromstring(archive.read(relpath))}
            root = ET.fromstring(archive.read('word/document.xml'))
            for number, para in enumerate(root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'), 1):
                context = ''.join(para.itertext()).strip()[:500]
                for node in para.iter():
                    for key, value in node.attrib.items():
                        rel = relations.get(value, {}) if key.endswith('}id') or key.endswith('}embed') else {}
                        url = rel.get('Target', '')
                        if rel.get('TargetMode') == 'External' and safe_link(url):
                            links.append({'url': url, 'paragraph': number, 'context': context})
                        if key.endswith('}embed'):
                            for figure in figures:
                                if 'word/' + url.removeprefix('./') == figure['source_part']:
                                    figure.update(paragraph=number, context=context)
    return figures, links


def inspect_pdf(data):
    from pypdf import PdfReader
    import pypdfium2
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted or len(reader.pages) > 300:
        raise ValueError('Encrypted PDF or over 300 pages')
    figures, links = [], []
    total = 0
    with pypdfium2.PdfDocument(data) as rendered:
        for number, page in enumerate(reader.pages, 1):
            context = (page.extract_text() or '').strip()[:500]
            for annotation in page.get('/Annots', []):
                uri = str(annotation.get_object().get('/A', {}).get('/URI', ''))
                if safe_link(uri):
                    links.append({'url': uri, 'page': number, 'context': context})
            width, height = rendered[number-1].get_size()
            if width * height * 2.25 > 25_000_000:
                raise ValueError('PDF page exceeds preview pixel limit')
            image = rendered[number-1].render(scale=1.5).to_pil()
            stream = io.BytesIO(); image.save(stream, format='PNG')
            total += stream.tell()
            if total > MAX_TOTAL:
                raise ValueError('PDF previews exceed batch limit')
            figures.append({'kind': 'page_preview', 'page': number, 'context': context,
                            'width': image.width, 'height': image.height,
                            'data': stream.getvalue(), 'suffix': '.png'})
    return figures, links


def index_references(manifest_path, out):
    manifest_path, out = Path(manifest_path).resolve(strict=True), Path(out)
    if out.exists():
        raise ValueError('Output already exists; reuse its index or choose a new directory')
    target = out.resolve()
    if target.is_relative_to(Path(__file__).resolve().parents[1]) or '/.codex/plugins/cache/' in target.as_posix().lower() + '/':
        raise ValueError('Keep reference assets outside the plugin and cache')
    refs = json.loads(manifest_path.read_text(encoding='utf-8-sig'))['references']
    if not 1 <= len(refs) <= 30:
        raise ValueError('Select 1–30 references')
    files, records, total = [], [], 0
    for number, ref in enumerate(refs, 1):
        source = (manifest_path.parent / ref['original']).resolve(strict=True)
        if not source.is_file() or source.stat().st_size > MAX_FILE:
            raise ValueError('Reference exceeds file limit')
        data = source.read_bytes(); digest = hashlib.sha256(data).hexdigest()
        if digest != ref['sha256']:
            raise ValueError('Reference hash differs from selected manifest')
        figures, links = (inspect_docx(data) if source.suffix.lower() == '.docx' else
                          inspect_pdf(data) if source.suffix.lower() == '.pdf' else ([], []))
        row = {'name': ref['name'], 'original': str(source), 'sha256': digest, 'figures': [], 'links': links}
        for index, figure in enumerate(figures, 1):
            content, suffix = figure.pop('data'), figure.pop('suffix')
            total += len(content)
            if total > MAX_TOTAL or len(files) >= MAX_ASSETS:
                raise ValueError('Reference figures exceed batch limit; select a smaller pack')
            name = f'{number:03d}/{index:03d}{suffix}'
            figure.update(file=name, sha256=hashlib.sha256(content).hexdigest())
            files.append((name, content)); row['figures'].append(figure)
        records.append(row)
    out.mkdir(parents=True, exist_ok=False)
    for name, content in files:
        path = out/name; path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
    index = {'version': 1, 'manifest': str(manifest_path), 'references': records,
             'link_policy': 'Candidates only: no links downloaded or executed; inspect and select relevant examples',
             'figure_policy': 'Reference examples, never current-client evidence; review and redact before inclusion'}
    (out/'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
    return index


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    index = index_references(args.manifest, args.out)
    print(json.dumps({'index': str(args.out.resolve()/'index.json'),
                      'figures': sum(len(x['figures']) for x in index['references']),
                      'links': sum(len(x['links']) for x in index['references'])}))
