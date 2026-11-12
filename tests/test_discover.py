import datetime as dt
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discover import aggregate, canonical, prepare, rule_extract, validate_entities, render


def document(**overrides):
    return {'id': 'doc1', 'url': 'https://example.com/article',
            'title': '뷰티 브랜드 ‘테스트공방’ 신제품 출시',
            'text': '브랜드 ‘테스트공방’이 첫 제품을 선보였다.',
            'published_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'publisher': '매체A', 'channel': 'test', 'query': '뷰티 브랜드 출시',
            'text_scope': 'title_and_search_snippet', **overrides}


class DiscoveryTests(unittest.TestCase):
    def test_duplicate_search_hits_are_one_document(self):
        a = document(url='https://example.com/article?utm_source=x')
        b = document(query='다른 검색')
        docs, _, _ = prepare([a, b], 90, 10)
        self.assertEqual(len(docs), 1)
        self.assertEqual(len(docs[0]['queries']), 2)

    def test_old_and_unknown_dates_not_called_recent(self):
        docs, rejected, _ = prepare([document(published_at='2000-01-01'),
                                     document(published_at='unknown'), document()], 90, 10)
        self.assertEqual(len(docs), 1)
        self.assertEqual(rejected['outside_window'], 1)
        self.assertEqual(rejected['unknown_date'], 1)

    def test_ai_fabrication_rejected(self):
        records = [{'document_id': 'doc1', 'name': '테스트공방',
                    'quote': '브랜드 ‘테스트공방’이 첫 제품을 선보였다.'},
                   {'document_id': 'doc1', 'name': '없는브랜드', 'quote': '없는브랜드 대박'},
                   {'document_id': 'unknown', 'name': '테스트공방', 'quote': '테스트공방'}]
        accepted, rejected = validate_entities(records, [document()])
        self.assertEqual((len(accepted), rejected), (1, 2))

    def test_no_entity_from_generic_quoted_slogan(self):
        self.assertEqual(rule_extract(document(title='“매일 행복하게” 새로운 유행', text='')), [])

    def test_generic_industry_and_host_are_not_entrants(self):
        self.assertEqual(rule_extract(document(title='식품업계, 신제품 출시', text='')), [])
        names = [r['name'] for r in rule_extract(document(
            title='테스트공방, 뷰티 편집숍 ‘호스트숍’ 입점', text=''))]
        self.assertIn('테스트공방', names)
        self.assertNotIn('호스트숍', names)

    def test_repeated_quotes_do_not_inflate_document_count(self):
        d = document(); entities = rule_extract(d)
        with sqlite3.connect(':memory:') as db:
            first = aggregate([(d, e) for e in entities] * 3, db)
            second = aggregate([(d, e) for e in entities], db)
        self.assertEqual(first[0]['retrieved_document_count'], 1)
        self.assertTrue(first[0]['new_to_registry'])
        self.assertFalse(second[0]['new_to_registry'])

    def test_url_canonicalization_preserves_article_identity(self):
        self.assertNotEqual(canonical('https://x.test/?id=1'), canonical('https://x.test/?id=2'))

    def test_html_escapes_untrusted_source(self):
        report = {'topics': ['<script>alert(1)</script>'], 'document_count': 0,
                  'extractor': 'rule', 'errors': [], 'candidates': []}
        self.assertNotIn('<script>', render(report))


if __name__ == '__main__': unittest.main()
