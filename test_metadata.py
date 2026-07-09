"""Unit tests for Libation-style metadata path helpers."""
import os
import unittest

from libby_download import (
    build_book_download_dir,
    display_name_to_file_as,
    parse_file_as,
    parse_overdrive_api_payload,
    sanitize_filename,
)


class TestSanitizeFilename(unittest.TestCase):
    def test_strips_colons(self):
        self.assertEqual(sanitize_filename('Dune: The Duke'), 'Dune The Duke')

    def test_strips_other_unsafe_chars(self):
        self.assertEqual(sanitize_filename('A/B:C'), 'A BC')


class TestFileAs(unittest.TestCase):
    def test_display_name_to_file_as(self):
        self.assertEqual(display_name_to_file_as('Gregory Maguire'), 'Maguire, Gregory')

    def test_parse_file_as(self):
        parts = parse_file_as('Maguire, Gregory')
        self.assertEqual(parts['last'], 'Maguire')
        self.assertEqual(parts['first'], 'Gregory')


class TestOverdriveParser(unittest.TestCase):
    def test_bulk_payload(self):
        payload = {
            'titles': [{
                'id': 2943031,
                'title': 'Wicked',
                'series': 'Wicked Years',
                'readingOrder': '1',
                'creators': [
                    {'role': 'Author', 'name': 'Gregory Maguire', 'fileAs': 'Maguire, Gregory'},
                    {'role': 'Narrator', 'name': 'Someone Else'},
                ],
            }]
        }
        meta = parse_overdrive_api_payload(payload, title_id='2943031')
        self.assertEqual(meta['author_file_as'], 'Maguire, Gregory')
        self.assertEqual(meta['series_name'], 'Wicked Years')
        self.assertEqual(meta['series_index'], '1')

    def test_bulk_array_with_detailed_series(self):
        payload = [{
            'id': '2943031',
            'title': 'Wicked',
            'series': 'Wicked Years',
            'detailedSeries': {
                'seriesName': 'Wicked Years',
                'readingOrder': '1',
            },
            'creators': [
                {'role': 'Author', 'name': 'Gregory Maguire', 'sortName': 'Maguire, Gregory'},
            ],
        }]
        meta = parse_overdrive_api_payload(payload, title_id='2943031')
        self.assertEqual(meta['author_file_as'], 'Maguire, Gregory')
        self.assertEqual(meta['series_index'], '1')

    def test_cover_url_from_covers(self):
        payload = [{
            'id': '2943031',
            'title': 'Wicked',
            'covers': {
                'cover300Wide': {'href': 'https://img.example/cover300.jpg'},
                'cover150Wide': {'href': 'https://img.example/cover150.jpg'},
            },
        }]
        meta = parse_overdrive_api_payload(payload, title_id='2943031')
        self.assertEqual(meta['cover_url'], 'https://img.example/cover300.jpg')


class TestBuildPath(unittest.TestCase):
    def test_series_path(self):
        base = '/downloads'
        meta = {
            'title': 'Wicked',
            'author_file_as': 'Maguire, Gregory',
            'series_name': 'Wicked Years',
            'series_index': '1',
        }
        path = build_book_download_dir(base, meta, 'Wicked')
        self.assertEqual(path, os.path.join(base, 'Maguire, Gregory', 'Wicked Years', '1_Wicked'))

    def test_no_series(self):
        base = '/downloads'
        meta = {
            'title': 'Vilest Things',
            'author_file_as': 'Gong, Chloe',
            'series_name': '',
            'series_index': '',
        }
        path = build_book_download_dir(base, meta, 'Vilest Things')
        self.assertEqual(path, os.path.join(base, 'Gong, Chloe', 'Vilest Things'))

    def test_colon_in_title(self):
        base = '/downloads'
        meta = {
            'title': 'Something: Subtitle',
            'author_file_as': 'Author, Test',
            'series_name': '',
            'series_index': '',
        }
        path = build_book_download_dir(base, meta, 'Something: Subtitle')
        self.assertNotIn(':', path)


if __name__ == '__main__':
    unittest.main()
