import copy
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ops', ROOT / 'tools/dictionary_ops.py')
ops = importlib.util.module_from_spec(spec); spec.loader.exec_module(ops)

JAN = '4573102725301'
ALIAS = '4573102691910'
NAME = 'HG 1/144 確認用商品'
MANUAL = 'https://manual.bandai-hobby.net/menus/detail/4956'

def catalog():
    return {'schema_version': 1, 'dictionary_version': '2026-10-15.1',
            'products': [{'product_id': 'stable-1', 'name': NAME, 'release_date': None,
                          'official_manual_url': MANUAL, 'grade': 'HG', 'scale': '1/144'}],
            'barcodes': {ALIAS: {'product_id': 'stable-1', 'verification': 'official_match'}}}

def listing(jan=JAN, name=NAME):
    return '<div class="m_cardA"><a href="/products/search/detail.php?prd_id=' + jan + '000&amp;grp_id=5325"></a><div class="m_cardA__date">2026年10月01日</div><div class="m_cardA__title">' + name + '</div>'

def manual(name=NAME, detail='4956', date='2026年10月01日'):
    return '<div class="bl_result_item"><a href="/menus/detail/' + detail + '">説明書</a><div class="bl_result_name">' + name + '<span>HG</span></div>' + date + '</section>'

class OperationsTests(unittest.TestCase):
    def report(self, base=None, manual_html=None):
        base = base or catalog()
        raw = ops.encoded(base)
        def fetch(url): return listing() if url == ops.LIST_URL else (manual_html or manual())
        return base, raw, ops.collect(base, ops.digest(raw), fetch)

    def test_checksum(self):
        self.assertTrue(ops.valid_jan(JAN)); self.assertFalse(ops.valid_jan(JAN[:-1] + '0'))
        self.assertFalse(ops.valid_jan('４５７３１０２７２５３０１'))

    def test_no_inferred_release_day(self):
        self.assertIsNone(ops.release_date('2026年10月発売予定'))
        self.assertIsNone(ops.release_date('2026年02月30日'))

    def test_listing_and_unknown_format(self):
        row = ops.parse_list(listing())[0]
        self.assertEqual(JAN, row['jan']); self.assertIn('&grp_id=', row['candidate_source_url'])
        with self.assertRaises(ValueError): ops.parse_list('<html>new format</html>')

    def test_manual_no_results_vs_parse_failure(self):
        self.assertEqual([], ops.parse_manual('検索結果 0件'))
        self.assertEqual([], ops.parse_manual('<p class="bl_result_num">条件に一致する商品は見つかりませんでした</p>'))
        with self.assertRaises(ValueError): ops.parse_manual('<html>error</html>')

    def test_ambiguous_results_are_held(self):
        _, _, report = self.report(manual_html=manual() + manual(detail='4957'))
        self.assertFalse(report['items'][0]['eligible'])

    def test_name_mismatch_is_held(self):
        _, _, report = self.report(manual_html=manual(name=NAME + '限定仕様'))
        self.assertFalse(report['items'][0]['eligible'])

    def test_alias_reuses_stable_product(self):
        base, raw, report = self.report()
        blob, manifest = ops.build_release(base, raw, report, [report['items'][0]['id']])
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            data = archive.read(manifest['dictionary_file'])
            result = json.loads(data)
        self.assertEqual(1, len(result['products']))
        self.assertEqual('stable-1', result['barcodes'][JAN]['product_id'])
        self.assertEqual(ops.digest(data), manifest['sha256'])
        self.assertEqual('2026-10-01', result['products'][0]['release_date'])
        self.assertEqual(base['barcodes'][ALIAS], result['barcodes'][ALIAS])

    def test_same_manual_multiple_new_jans_share_product(self):
        base = catalog(); base['products'][0]['official_manual_url'] = None
        _, raw, report = self.report(base)
        other = copy.deepcopy(report['items'][0]); other['jan'] = '4573102693617'
        other['source_url'] = 'https://manual.bandai-hobby.net/?freeword=' + other['jan']
        other['id'] = ops.fingerprint(other); report['items'].append(other)
        blob, manifest = ops.build_release(base, raw, report, [x['id'] for x in report['items']])
        result = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read(manifest['dictionary_file']))
        self.assertEqual(2, len(result['products']))
        self.assertEqual(result['barcodes'][JAN]['product_id'], result['barcodes'][other['jan']]['product_id'])

    def test_product_identity_conflict_is_held(self):
        base = catalog(); base['products'].append(dict(base['products'][0], product_id='stable-2'))
        _, _, report = self.report(base)
        self.assertFalse(report['items'][0]['eligible'])

    def test_approval_bound_to_base_and_content(self):
        base, raw, report = self.report(); key = report['items'][0]['id']
        with self.assertRaises(ValueError): ops.build_release(base, raw + b' ', report, [key])
        report['items'][0]['name'] += '改変'
        with self.assertRaises(ValueError): ops.build_release(base, raw, report, [key])

    def test_unconfirmed_cannot_be_exported(self):
        base, raw, report = self.report(manual_html='検索結果 0件')
        with self.assertRaises(ValueError): ops.build_release(base, raw, report, [report['items'][0]['id']])
        with self.assertRaises(ValueError): ops.build_release(base, raw, report, [])

    def test_compatibility_and_version(self):
        base, raw, report = self.report()
        with self.assertRaises(ValueError): ops.build_release(base, raw, report, [report['items'][0]['id']], version=base['dictionary_version'])
        changed = copy.deepcopy(base); changed['barcodes'][ALIAS]['product_id'] = 'missing'
        with self.assertRaises(ValueError): ops.validate(changed, base)

    def test_recheck_rejects_forged_provenance(self):
        _, _, report = self.report(); row = report['items'][0]
        ops.recheck([row], lambda _: manual())
        row['candidate_source_url'] = row['candidate_source_url'].replace(JAN, ALIAS)
        with self.assertRaises(ValueError): ops.recheck([row], lambda _: manual())

    def test_official_response_changed(self):
        _, _, report = self.report()
        with self.assertRaises(ValueError): ops.recheck(report['items'], lambda _: manual(date='2026年10月02日'))

    def test_url_boundaries(self):
        for url in ['http://manual.bandai-hobby.net/', 'https://manual.bandai-hobby.net.evil.test/',
                    'https://user:password@manual.bandai-hobby.net/', 'https://127.0.0.1/', 'https://manual.bandai-hobby.net:123/']:
            with self.assertRaises(ValueError): ops.checked_url(url)

    def test_real_catalog_preserves_existing_mappings(self):
        remote = ops.read(ROOT / 'gunpla_dictionary.json')
        ops.validate(remote)
        if (ROOT / 'dictionary_manifest.json').exists():
            manifest = ops.read(ROOT / 'dictionary_manifest.json')
            self.assertEqual(ops.digest((ROOT / 'gunpla_dictionary.json').read_bytes()), manifest['sha256'])

if __name__ == '__main__': unittest.main()
