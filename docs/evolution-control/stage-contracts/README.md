# Stage Contract Locks

Each v2 stage freezes its immutable task contract here before implementation. The matching stage report records the lock path, SHA-256 fingerprint, approval reference, and the Git commit that first stored the lock.

Mutable execution status stays in the stage report. Task IDs, source intent IDs, mandatory/optional classification, acceptance criteria, required evidence, and source-task dispositions must continue to match the frozen lock. A contract revision requires explicit user approval and a new revision record; editing both the report and lock without that authority is not a valid revision.
