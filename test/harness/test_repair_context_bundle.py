import unittest

from services.repair_context_bundle import build_repair_context_bundle


class RepairContextBundleTests(unittest.TestCase):
    def test_bundle_collects_target_and_related_evidence(self):
        bundle = build_repair_context_bundle({
            "crash_diagnosis": {"file": "src/a.cpp", "line": 12, "function": "Foo::run", "category": "uaf"},
            "code_context": {
                "candidate_nodes": [{"file": "src/a.cpp", "function_signature": "Foo::run"}],
                "callers": [{"file": "src/b.cpp", "symbol": "Worker::tick"}],
                "fields": [{"file": "src/a.h", "symbol": "resource_"}],
                "tests": [{"file": "tests/foo_test.cpp", "symbol": "FooTest"}],
            },
        }, context_session={"context_session_hash": "ctx1"},
        authorized_scope={"code_roots": ["src"]})
        self.assertEqual(bundle["target"]["function"], "Foo::run")
        self.assertEqual(bundle["call_chain"][0]["symbol"], "Worker::tick")
        self.assertEqual(bundle["authorized_scope"]["code_roots"], ["src"])
        self.assertEqual(bundle["provenance"]["context_session_hash"], "ctx1")

    def test_bundle_is_bounded(self):
        rows = [{"file": "src/a.cpp", "content": "x" * 10000} for _ in range(40)]
        bundle = build_repair_context_bundle({"code_context": {"candidate_nodes": rows}}, max_chars=1000)
        self.assertLessEqual(len(bundle["related_symbols"]), 8)

    def test_bundle_reads_existing_graph_shape(self):
        bundle = build_repair_context_bundle({"code_context": {"graph": {"nodes": [
            {"file": "src/a.cpp", "signature": "Foo::run", "snippet": ["void Foo::run() {}"]}
        ], "edges": [{"type": "calls_direct", "from_id": "Worker::tick", "to_id": "Foo::run"}]}}})
        self.assertEqual(bundle["target"]["function"], "Foo::run")
        self.assertEqual(bundle["related_symbols"][0]["signature"], "Foo::run")
        self.assertEqual(bundle["call_chain"][0]["type"], "calls_direct")


if __name__ == "__main__":
    unittest.main()
