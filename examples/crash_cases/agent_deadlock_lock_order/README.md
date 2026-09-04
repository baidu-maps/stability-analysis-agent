# Native lock-order deadlock

The thread dump contains a cycle between Cache and Worker locks. This static fixture tests multi-thread evidence and prevents a null-pointer-only diagnosis.
