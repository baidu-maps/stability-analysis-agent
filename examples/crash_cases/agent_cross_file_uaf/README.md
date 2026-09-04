# Cross-file asynchronous UAF

The stack points at `Session::onCallback`; the release path lives in `resource.cpp` and the queued callback in `callback_queue.cpp`. This fixture is static-only and is intended to test ContextEngine requests, RepoMap ranking, and evidence provenance.
