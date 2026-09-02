import unittest

from uma8_visualizer.json_stream import JSONStreamParser, parse_chunks


class JSONStreamTests(unittest.TestCase):
    def test_single_multiline_object(self) -> None:
        result = list(parse_chunks(['{\n "timeStamp": 1,\n "src": []\n}\n']))
        self.assertEqual(result, [{"timeStamp": 1, "src": []}])

    def test_consecutive_objects(self) -> None:
        result = list(parse_chunks(['{"timeStamp":1}{"timeStamp":2}']))
        self.assertEqual([item["timeStamp"] for item in result], [1, 2])

    def test_logs_around_json(self) -> None:
        result = list(parse_chunks(['ODAS started\n{"src": []}\nnormal log']))
        self.assertEqual(result, [{"src": []}])

    def test_arbitrary_chunks(self) -> None:
        parser = JSONStreamParser()
        result = []
        for chunk in ('noise {"ti', 'meStamp": 3, "src":', ' [{"id": 7}]} trailing'):
            result.extend(parser.feed(chunk))
        self.assertEqual(result[0]["src"][0]["id"], 7)

    def test_recovers_after_damaged_json(self) -> None:
        result = list(parse_chunks(['{"bad":,}\n', 'log\n{"timeStamp":4,"src":[]}']))
        self.assertEqual(result, [{"timeStamp": 4, "src": []}])

    def test_braces_inside_string(self) -> None:
        result = list(parse_chunks(['{"message":"a } and { b","src":[]}']))
        self.assertEqual(result[0]["message"], "a } and { b")


if __name__ == "__main__":
    unittest.main()
