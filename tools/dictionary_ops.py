"""Reviewed JAN collection and local operations UI; Python standard library only."""
import argparse
import copy
import datetime as dt
import hashlib
import html
import io
import json
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AGENT = 'TsumiplaDictionaryBot/1.0 (+mailto:mabumie1014@gmail.com)'
LIST_URL = 'https://www.bandaispirits.co.jp/products/search/result.php?search=true&category=1&sort=salesdate&order=decs&dsp_cnt=60'
ALLOWED_HOSTS = {'www.bandaispirits.co.jp', 'manual.bandai-hobby.net'}
UTC = dt.timezone.utc

def now():
    return dt.datetime.now(UTC).isoformat()

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

def digest(value):
    return hashlib.sha256(value).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(encoded(value))
    temp.replace(path)

def valid_jan(jan):
    return bool(re.fullmatch('[0-9]{13}', jan)) and sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(jan)) % 10 == 0

def normalized(name):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', name)).casefold()

def clean(value):
    return html.unescape(re.sub('<[^>]*>', '', value)).strip()

def release_date(text):
    match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if not match:
        return None
    try:
        return dt.date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None

def grade_of(name):
    match = re.match(r'^(MGEX|MGSD|HG[A-Z]*|RG|MG|PG|SD[A-Z]*|EG)\b', unicodedata.normalize('NFKC', name))
    return match[1] if match else None

def checked_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('許可された公式サイトのHTTPS URLではありません')
    return parsed

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = checked_url(newurl)
        if target.netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise ValueError('公式サイトから別のホストへの転送は追跡しません')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class Fetcher:
    def __init__(self, cache, interval=3):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.last = {}
        self.robots = {}
        self.requests = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def raw(self, url):
        parsed = checked_url(url)
        delay = max(self.interval, self.robots.get(parsed.netloc, (None, 0))[1])
        time.sleep(max(0, delay - (time.monotonic() - self.last.get(parsed.netloc, 0))))
        self.last[parsed.netloc] = time.monotonic()
        self.requests += 1
        request = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': 'text/html,text/plain'})
        with self.opener.open(request, timeout=25) as response:
            data = response.read(3_000_001)
            if len(data) > 3_000_000:
                raise ValueError('収集ページがサイズ上限を超えました')
            return data.decode(response.headers.get_content_charset() or 'utf-8')

    def get(self, url):
        parsed = checked_url(url)
        if parsed.netloc not in self.robots:
            robot = urllib.robotparser.RobotFileParser()
            try:
                text = self.raw('https://' + parsed.netloc + '/robots.txt')
            except urllib.error.HTTPError as error:
                if error.code not in (404, 410):
                    raise
                text = ''
            robot.parse(text.splitlines())
            self.robots[parsed.netloc] = (robot, robot.crawl_delay(AGENT) or robot.crawl_delay('*') or 0)
        robot = self.robots[parsed.netloc][0]
        if not robot.can_fetch(AGENT, url):
            raise ValueError('robots.txtで収集が許可されていません：' + parsed.netloc)
        cache = self.cache / (digest(url.encode()) + '.json')
        if cache.exists():
            record = read(cache)
            if time.time() - record['time'] < 20 * 3600 and record['url'] == url:
                return record['body']
        body = self.raw(url)
        atomic(cache, {'url': url, 'time': time.time(), 'body': body})
        return body

def parse_list(text):
    items = []
    for block in text.split('<div class="m_cardA">')[1:]:
        link = re.search(r'href="([^"]*detail\.php\?[^"]+)"', block)
        title = re.search(r'<div class="m_cardA__title">(.*?)</div>', block, re.S)
        if not link or not title:
            continue
        url = urllib.parse.urljoin(LIST_URL, html.unescape(link[1]))
        prd = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('prd_id', [''])[0]
        if not re.fullmatch('[0-9]{16}', prd):
            continue
        # This is a candidate extracted from the official product identifier,
        # not a verified JAN until a separate manual lookup matches it.
        jan = prd[:13]
        if not valid_jan(jan):
            continue
        date = re.search(r'<div class="m_cardA__date">(.*?)</div>', block, re.S)
        items.append({'jan': jan, 'listed_name': clean(title[1]), 'candidate_source_url': url,
                      'listed_release_date': release_date(clean(date[1])) if date else None})
    if not items:
        raise ValueError('公式商品一覧を解析できません。0件を正常扱いせず、形式変更を確認してください')
    return list({(x['jan'], x['candidate_source_url']): x for x in items}.values())

def parse_manual(text):
    matches = []
    for block in re.findall(r'<div class="bl_result_item">(.*?)(?=<div class="bl_result_item">|</section>|<div class="bl_paging)', text, re.S):
        detail = re.search(r'href="/menus/detail/(\d+)"', block)
        name = re.search(r'<div class="bl_result_name">\s*(.*?)<span', block, re.S)
        if detail and name:
            matches.append({'name': clean(name[1]), 'official_manual_url': 'https://manual.bandai-hobby.net/menus/detail/' + detail[1],
                            'release_date': release_date(block)})
    if not matches and not re.search(r'0\s*件|条件に一致する商品は見つかりませんでした', text):
        raise ValueError('公式説明書の検索結果を解析できません')
    return list({(x['official_manual_url'], x['name']): x for x in matches}.values())

def validate(catalog, old=None):
    if catalog.get('schema_version') != 1 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.\d+', catalog.get('dictionary_version', '')):
        raise ValueError('辞書の形式・バージョンが不正です')
    products = {}
    for product in catalog['products']:
        pid = product['product_id']
        if not pid or pid in products or not product.get('name', '').strip():
            raise ValueError('商品IDまたは商品名が不正・重複です')
        if product.get('release_date'):
            dt.date.fromisoformat(product['release_date'])
        products[pid] = product
    if not products:
        raise ValueError('空の辞書は配信しません')
    for jan, row in catalog['barcodes'].items():
        if not valid_jan(jan) or row['product_id'] not in products or row.get('verification') not in ('official_match', 'prior_official_match', 'reviewed_match'):
            raise ValueError('JAN対応が不正・未確認です')
    if old:
        if not set(products).issuperset(p['product_id'] for p in old['products']):
            raise ValueError('既存の商品IDは削除できません')
        for jan, row in old['barcodes'].items():
            if catalog['barcodes'].get(jan, {}).get('product_id') != row['product_id']:
                raise ValueError('既存JANの対応は変更できません')

def fingerprint(row):
    return digest(encoded({k: v for k, v in row.items() if k != 'id'}))[:24]

def collect(catalog, base_hash, fetch, limit=30):
    validate(catalog)
    products = {p['product_id']: p for p in catalog['products']}
    manual_ids = {}
    for product in products.values():
        if product.get('official_manual_url'):
            manual_ids.setdefault(product['official_manual_url'], set()).add(product['product_id'])
    report = {'schema_version': 1, 'base_sha256': base_hash, 'base_version': catalog['dictionary_version'],
              'collected_at': now(), 'source_url': LIST_URL, 'items': [], 'errors': [],
              'scanned': 0, 'already_known': 0, 'remaining': 0}
    listed = parse_list(fetch(LIST_URL))
    report['scanned'] = len(listed)
    # Rotate the bounded window so unresolved future products do not starve later rows.
    offset = dt.datetime.now(UTC).date().toordinal() * limit % len(listed)
    listed = listed[offset:] + listed[:offset]
    checks = 0
    for row in listed:
        known = catalog['barcodes'].get(row['jan'])
        old = products.get(known['product_id']) if known else None
        if old and old.get('release_date'):
            report['already_known'] += 1
            continue
        if checks >= limit:
            report['remaining'] += 1
            continue
        checks += 1
        row['source_url'] = 'https://manual.bandai-hobby.net/?freeword=' + row['jan']
        row.update(eligible=False, reason='公式説明書での照合待ち', kind='発売日補完' if known else 'JAN追加')
        try:
            matches = parse_manual(fetch(row['source_url']))
            if len(matches) != 1:
                row['reason'] = '公式説明書の一致が0件または複数です'
            else:
                row.update(matches[0])
                aliases = manual_ids.get(row['official_manual_url'], set())
                if len(aliases) > 1 or (known and aliases and known['product_id'] not in aliases):
                    row['reason'] = '既存の商品IDと説明書の対応が競合しています'
                elif normalized(row['listed_name']) != normalized(row['name']):
                    row['reason'] = '商品一覧と説明書の商品名が一致しません'
                elif known and old.get('official_manual_url') != row['official_manual_url']:
                    row['reason'] = '既存JANの説明書URLを確認できません'
                elif known and not row.get('release_date'):
                    report['already_known'] += 1
                    continue
                else:
                    row['target_product_id'] = known['product_id'] if known else (next(iter(aliases)) if aliases else None)
                    row['eligible'] = True
                    row['reason'] = '公式説明書の単一結果と商品名を照合済み。運営者の確認待ち'
        except Exception as error:
            row['reason'] = '収集エラー：' + str(error)
            report['errors'].append({'jan': row['jan'], 'error': str(error)})
        row['id'] = fingerprint(row)
        report['items'].append(row)
    return report

def version_key(version):
    day, seq = version.rsplit('.', 1)
    return (dt.date.fromisoformat(day), int(seq))

def recheck(rows, fetch):
    for row in rows:
        jan = row['jan']
        query = 'https://manual.bandai-hobby.net/?freeword=' + jan
        if row.get('source_url') != query:
            raise ValueError('照合元がJANの公式検索と一致しません')
        source = checked_url(row['candidate_source_url'])
        prd = urllib.parse.parse_qs(source.query).get('prd_id', [''])[0]
        if source.hostname != 'www.bandaispirits.co.jp' or source.path != '/products/search/detail.php' or not re.fullmatch('[0-9]{16}', prd) or prd[:13] != jan:
            raise ValueError('公式の商品コードと候補JANが一致しません')
        matches = parse_manual(fetch(query))
        if len(matches) != 1 or matches[0]['official_manual_url'] != row['official_manual_url'] or normalized(matches[0]['name']) != normalized(row['name']) or matches[0]['release_date'] != row.get('release_date'):
            raise ValueError('公式情報が候補から変わっています。再収集してください')

def public_json(url):
    prefix = 'https://raw.githubusercontent.com/karasho07/tsumipla-dictionary/'
    if not url.startswith(prefix):
        raise ValueError('辞書配信リポジトリ以外には接続しません')
    request = urllib.request.Request(url, headers={'User-Agent': AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(8_000_001)
        if len(data) > 8_000_000: raise ValueError('配信データが大きすぎます')
        return data

def synchronize(repo, report_path):
    prefix = 'https://raw.githubusercontent.com/karasho07/tsumipla-dictionary/'
    manifest = json.loads(public_json(prefix + 'main/dictionary_manifest.json'))
    leaf = manifest['dictionary_file']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.json', leaf): raise ValueError('配信ファイル名が不正です')
    data = public_json(prefix + 'main/' + leaf)
    catalog = json.loads(data)
    old = read(repo / 'gunpla_dictionary.json')
    validate(catalog, old)
    if digest(data) != manifest['sha256'] or catalog['dictionary_version'] != manifest['dictionary_version'] or len(catalog['products']) != manifest['product_count'] or len(catalog['barcodes']) != manifest['barcode_count']:
        raise ValueError('配信辞書とマニフェストが一致しません')
    if version_key(catalog['dictionary_version']) < version_key(old['dictionary_version']): raise ValueError('古い配信辞書には戻しません')
    if version_key(catalog['dictionary_version']) == version_key(old['dictionary_version']) and encoded(catalog) != encoded(old): raise ValueError('同じバージョンの辞書内容が変わっています')
    # Preserve the byte representation because candidate approvals bind to its SHA-256.
    temp = repo / '.operator/synchronized.json'
    temp.write_bytes(data); temp.replace(repo / 'gunpla_dictionary.json')
    atomic(repo / 'dictionary_manifest.json', manifest)
    try:
        report = json.loads(public_json(prefix + 'jan-candidates/reports/latest.json'))
    except urllib.error.HTTPError as error:
        if error.code == 404: return '最新辞書を取得しました。毎日の収集はまだ未稼働です。手動収集を利用できます。'
        raise
    if report.get('base_sha256') != digest(data): return '最新辞書を取得しました。保存済みの収集候補は旧版用です。「公式情報を収集する」を実行してください。'
    if report.get('schema_version') != 1 or not isinstance(report.get('items'), list): raise ValueError('候補ファイルの形式が不正です')
    for row in report['items']:
        if row.get('id') != fingerprint(row): raise ValueError('候補ファイルが不正です')
    atomic(report_path, report)
    return '最新辞書と毎日の収集候補を取得しました。'

def build_release(catalog, raw_base, report, selected, version=None):
    validate(catalog)
    if report.get('base_sha256') != digest(raw_base):
        raise ValueError('辞書が収集時から変わっています。候補を再収集してください')
    if not selected or len(selected) != len(set(selected)):
        raise ValueError('承認する候補を選択してください（重複不可）')
    ids = {row['id']: row for row in report['items']}
    if any(key not in ids for key in selected):
        raise ValueError('候補IDが見つかりません')
    result = copy.deepcopy(catalog)
    products = {p['product_id']: p for p in result['products']}
    processed_jans = set()
    for key in selected:
        row = ids[key]
        if not row.get('eligible') or row['id'] != fingerprint(row) or not valid_jan(row['jan']):
            raise ValueError('未確認・改変された候補は配信できません')
        checked_url(row['source_url']); checked_url(row['candidate_source_url'])
        if not re.fullmatch(r'https://manual\.bandai-hobby\.net/menus/detail/\d+', row['official_manual_url']):
            raise ValueError('説明書URLが不正です')
        if normalized(row['name']) != normalized(row['listed_name']):
            raise ValueError('商品名が照合できません')
        if row['jan'] in processed_jans:
            raise ValueError('同じJANの候補が複数あります')
        processed_jans.add(row['jan'])
        pid = row.get('target_product_id')
        if pid:
            if pid not in products or products[pid].get('official_manual_url') != row['official_manual_url']:
                raise ValueError('既存商品との対応が一致しません')
        else:
            aliases = [p['product_id'] for p in products.values() if p.get('official_manual_url') == row['official_manual_url']]
            if len(aliases) > 1:
                raise ValueError('既存の商品IDが競合しています')
            pid = aliases[0] if aliases else 'manual-auto-' + row['official_manual_url'].rsplit('/', 1)[1]
            if pid not in products:
                scale = re.search(r'1\s*/\s*\d+', unicodedata.normalize('NFKC', row['name']))
                product = {'product_id': pid, 'name': row['name'], 'grade': grade_of(row['name']),
                           'scale': re.sub(r'\s+', '', scale[0]) if scale else None, 'release_date': row.get('release_date'),
                           'image_url': None, 'official_manual_url': row['official_manual_url'], 'source_row': None}
                result['products'].append(product); products[pid] = product
            elif products[pid].get('official_manual_url') != row['official_manual_url']:
                raise ValueError('新しい商品IDが既存商品と衝突しました')
        if row.get('release_date') and not products[pid].get('release_date'):
            products[pid]['release_date'] = row['release_date']
        existing = result['barcodes'].get(row['jan'])
        if existing and existing['product_id'] != pid:
            raise ValueError('既存JANの対応が変わるため中止しました')
        if not existing:
            result['barcodes'][row['jan']] = {'product_id': pid, 'verification': 'reviewed_match',
                'source_url': row['source_url'], 'matched_name': row['name'], 'verified_at': dt.date.today().isoformat(),
                'candidate_source_url': row['candidate_source_url']}
    today = dt.date.today()
    old_day, old_seq = version_key(catalog['dictionary_version'])
    next_day = max(today, old_day)
    result['dictionary_version'] = version or f'{next_day.isoformat()}.{old_seq + 1 if next_day == old_day else 1}'
    if version_key(result['dictionary_version']) <= version_key(catalog['dictionary_version']):
        raise ValueError('バージョンを増やしてください')
    validate(result, catalog)
    data = encoded(result)
    name = 'gunpla_dictionary_' + result['dictionary_version'] + '.json'
    manifest = {'schema_version': 1, 'dictionary_version': result['dictionary_version'], 'dictionary_file': name,
                'sha256': digest(data), 'product_count': len(result['products']), 'barcode_count': len(result['barcodes']), 'pending_count': sum(not row.get('eligible') for row in report['items'])}
    audit = {'created_at': now(), 'base_sha256': digest(raw_base), 'result_sha256': digest(data),
             'version': result['dictionary_version'], 'approved': [ids[key] for key in selected]}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, data)
        output.writestr('gunpla_dictionary.json', data)
        output.writestr('dictionary_manifest.json', encoded(manifest))
        output.writestr('approval-audit-' + result['dictionary_version'] + '.json', encoded(audit))
    return archive.getvalue(), manifest

def server(repo, port, open_browser=True):
    repo = Path(repo).resolve()
    token = secrets.token_urlsafe(32)
    state_dir = repo / '.operator'
    state_dir.mkdir(exist_ok=True)
    lock = threading.Lock()
    job = {'busy': False, 'message': ''}
    allowed_host = '127.0.0.1:' + str(port)
    base_url = 'http://' + allowed_host
    report_path = state_dir / 'candidates.json'

    def run_collection():
        try:
            raw = (repo / 'gunpla_dictionary.json').read_bytes()
            result = collect(json.loads(raw), digest(raw), Fetcher(state_dir / 'cache').get)
            atomic(report_path, result)
            job['message'] = f'収集完了：{len(result["items"])}候補、要確認エラー{len(result["errors"])}件'
        except Exception as error:
            job['message'] = '収集に失敗しました（前回の候補は保持）: ' + str(error)
        finally:
            with lock: job['busy'] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def authorized(self):
            return self.headers.get('Host') == allowed_host and secrets.compare_digest(self.headers.get('X-Operator-Token', ''), token)

        def do_GET(self):
            if self.headers.get('Host') != allowed_host:
                return self.send(403, encoded({'error': 'Host not allowed'}))
            path = urllib.parse.urlsplit(self.path).path
            if path == '/':
                return self.send(200, (repo / 'tools/admin.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/admin.js':
                return self.send(200, (repo / 'tools/admin.js').read_bytes(), 'application/javascript; charset=utf-8')
            if not self.authorized():
                return self.send(403, encoded({'error': 'この起動用URLから開き直してください'}))
            if path == '/api/state':
                catalog = read(repo / 'gunpla_dictionary.json')
                decisions = read(state_dir / 'decisions.json') if (state_dir / 'decisions.json').exists() else {}
                return self.send(200, encoded({'job': job, 'version': catalog['dictionary_version'],
                    'products': len(catalog['products']), 'barcodes': len(catalog['barcodes']),
                    'report': read(report_path) if report_path.exists() else None, 'decisions': decisions}))
            return self.send(404, encoded({'error': 'Not found'}))

        def do_POST(self):
            if not self.authorized() or self.headers.get('Origin') != base_url:
                return self.send(403, encoded({'error': '操作元を確認できません'}))
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 2_000_000: raise ValueError('データが大きすぎます')
                value = json.loads(self.rfile.read(length))
                path = urllib.parse.urlsplit(self.path).path
                if path == '/api/collect':
                    with lock:
                        if job['busy']: raise ValueError('すでに収集中です')
                        job.update(busy=True, message='公式情報を収集中（最大数分）')
                    threading.Thread(target=run_collection, daemon=True).start()
                    return self.send(202, encoded({'ok': True}))
                if path == '/api/import':
                    # Only import a generated candidate report, never accept executable code or base catalogs.
                    if job['busy']: raise ValueError('収集中は取り込めません')
                    if value.get('schema_version') != 1 or not isinstance(value.get('items'), list): raise ValueError('候補ファイルではありません')
                    if value.get('base_sha256') != digest((repo / 'gunpla_dictionary.json').read_bytes()): raise ValueError('辞書が異なる候補です。現在の辞書で再収集してください')
                    for row in value['items']:
                        if row.get('id') != fingerprint(row): raise ValueError('候補が変更されています')
                    atomic(report_path, value)
                    return self.send(200, encoded({'ok': True}))
                if path == '/api/sync':
                    if job['busy']: raise ValueError('収集中は同期できません')
                    job['message'] = synchronize(repo, report_path)
                    return self.send(200, encoded({'ok': True}))
                if path == '/api/decision':
                    if job['busy']: raise ValueError('収集中は承認できません')
                    report = read(report_path)
                    ids = {row['id']: row for row in report['items']}
                    if value.get('decision') not in ('approved', 'held'): raise ValueError('選択が不正です')
                    decisions_path = state_dir / 'decisions.json'
                    decisions = read(decisions_path) if decisions_path.exists() else {}
                    for key in value['ids']:
                        if key not in ids or (value['decision'] == 'approved' and not ids[key]['eligible']): raise ValueError('未確認の候補は承認できません')
                        decisions[key] = value['decision']
                    atomic(decisions_path, decisions)
                    return self.send(200, encoded({'ok': True}))
                if path == '/api/export':
                    if job['busy']: raise ValueError('収集中は書き出せません')
                    report = read(report_path)
                    decisions = read(state_dir / 'decisions.json') if (state_dir / 'decisions.json').exists() else {}
                    selected = [row['id'] for row in report['items'] if decisions.get(row['id']) == 'approved']
                    recheck([row for row in report['items'] if row['id'] in selected], Fetcher(state_dir / 'cache').get)
                    raw = (repo / 'gunpla_dictionary.json').read_bytes()
                    live = json.loads(public_json('https://raw.githubusercontent.com/karasho07/tsumipla-dictionary/main/dictionary_manifest.json'))
                    if live.get('sha256') != digest(raw):
                        raise ValueError('配信中の辞書と一致しません。初回セットアップ、または「最新辞書と毎日の候補を取得」を実行して再確認してください')
                    archive, manifest = build_release(json.loads(raw), raw, report, selected)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Disposition', 'attachment; filename="tsumipla-dictionary-' + manifest['dictionary_version'] + '.zip"')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(archive)))
                    self.end_headers(); self.wfile.write(archive)
                    return
                raise ValueError('対応していない操作です')
            except (ValueError, KeyError, OSError, TypeError) as error:
                self.send(400, encoded({'error': str(error)}))

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = base_url + '/#' + token
    print('運営画面：' + url, flush=True)
    print('終了するにはこのウィンドウで Ctrl+C。GitHubへの送信は行いません。', flush=True)
    if open_browser: webbrowser.open(url)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally: httpd.server_close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['collect', 'serve', 'validate'])
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('reports/latest.json'))
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if args.command == 'serve': return server(args.repo, args.port, not args.no_browser)
    raw = (args.repo / 'gunpla_dictionary.json').read_bytes()
    if args.command == 'validate':
        validate(json.loads(raw)); print('辞書の検証に成功'); return
    if not 1 <= args.limit <= 60: parser.error('--limit must be 1..60')
    result = collect(json.loads(raw), digest(raw), Fetcher(args.repo / '.operator/cache').get, args.limit)
    atomic(args.output, result)
    print(json.dumps({'items': len(result['items']), 'eligible': sum(row['eligible'] for row in result['items']), 'errors': len(result['errors']), 'remaining': result['remaining']}, ensure_ascii=False))
    if result['errors']: raise SystemExit(2)

if __name__ == '__main__': main()
