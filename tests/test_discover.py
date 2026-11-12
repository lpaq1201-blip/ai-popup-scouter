import datetime as dt
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import json
import io
from contextlib import redirect_stderr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discover import aggregate, canonical, prepare, rule_extract, validate_entities, render, naver, main


def document(**overrides):
    return {'id': 'doc1', 'url': 'https://example.com/article',
            'title': '뷰티 브랜드 ‘테스트공방’ 신제품 출시',
            'text': '브랜드 ‘테스트공방’이 첫 제품을 선보였다.',
            'published_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'publisher': '매체A', 'channel': 'naver_blog', 'query': '뷰티 브랜드 출시',
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

    def test_news_excluded_even_during_offline_replay(self):
        rows = [document(channel='google_news_rss'), document(channel='naver_news'),
                document(channel='unknown'), document()]
        docs, rejected, _ = prepare(rows, 90, 100)
        self.assertEqual(len(docs), 1)
        self.assertEqual(rejected['excluded_source'], 3)

    @patch.dict('os.environ', {'NAVER_CLIENT_ID': 'test', 'NAVER_CLIENT_SECRET': 'test'})
    def test_blog_pagination_and_partial_page(self):
        pages = [json.dumps({'total': 250, 'items': [
            {'link': f'https://blog.naver.com/test/{i}', 'title': '후기', 'postdate': '20261112'}
            for i in range(offset, offset + size)]}).encode()
            for offset, size in [(0, 100), (100, 100), (200, 50)]]
        with patch('discover.request', side_effect=pages) as fetch:
            rows, stats, errors = naver('상품군 후기', 250)
        self.assertEqual(len(rows), 250)
        self.assertEqual(stats['page_requests'], 3)
        self.assertEqual(errors, [])
        self.assertIn('start=101', fetch.call_args_list[1].args[0])
        self.assertIn('display=50', fetch.call_args_list[2].args[0])
        self.assertTrue(all('/search/blog.json?' in c.args[0] for c in fetch.call_args_list))

    @patch.dict('os.environ', {'NAVER_CLIENT_ID': 'test', 'NAVER_CLIENT_SECRET': 'test'})
    def test_later_failure_preserves_first_page(self):
        first = json.dumps({'total': 500, 'items': [
            {'link': f'https://blog.naver.com/test/{i}'} for i in range(100)]}).encode()
        with patch('discover.request', side_effect=[first, RuntimeError('HTTP_429')]):
            rows, stats, errors = naver('후기', 500)
        self.assertEqual(len(rows), 100)
        self.assertEqual(stats['stop_reason'], 'page_error')
        self.assertEqual(errors[0]['page_start'], 101)
        self.assertEqual(errors[0]['error'], 'HTTP_429')

    @patch.dict('os.environ', {'NAVER_CLIENT_ID': 'test', 'NAVER_CLIENT_SECRET': 'test'})
    def test_repeated_page_not_counted_twice(self):
        page = json.dumps({'total': 1000, 'items': [
            {'link': f'https://blog.naver.com/test/{i}'} for i in range(100)]}).encode()
        with patch('discover.request', side_effect=[page, page]):
            rows, stats, _ = naver('후기', 500)
        self.assertEqual(len(rows), 100)
        self.assertEqual(stats['stop_reason'], 'repeated_page')

    def test_missing_credentials_never_falls_back_to_news(self):
        with patch.dict('os.environ', {}, clear=True), patch('sys.argv', ['discover.py']), \
                patch('discover.request') as fetch, redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as stopped: main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('뉴스 수집은 제외', error.getvalue())
        fetch.assert_not_called()

    def test_google_source_removed(self):
        with patch('sys.argv', ['discover.py', '--source', 'google']), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped: main()
        self.assertEqual(stopped.exception.code, 2)

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
