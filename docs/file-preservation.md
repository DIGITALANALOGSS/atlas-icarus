# Controlled local file preservation

This tool creates verified copies and a manifest outside the repository.
It makes no API requests, registers no intake or evidence records, and
does not submit jobs.

## Usage

Create an existing storage directory outside the Git repository. Use an
appropriate private location, not a public or shared folder.

```bash
python3 scripts/atlas_preserve.py /path/to/source-file \
  --destination /path/to/existing-private-storage \
  --custodian "Custodian name" \
  --source-label "Collection source" \
  --received-at "2026-10-08T12:00:00Z"
```

The tool creates a unique UUID directory containing:

- `preserved/content.bin`: byte-for-byte preserved copy.
- `working/content.bin`: separate working copy.
- `manifest.json`: successful verification record.

The generic copy names avoid using untrusted source filenames as output
paths. The manifest records the original filename, source path, source
label, custodian, received and collection times, copy paths, size and hash.

## Safety and limitations

- The source must be a regular file; final-component symlinks are rejected.
- Source content is never intentionally modified or deleted.
- Reading a file can affect filesystem access-time metadata.
- Copies are hashed and compared before success is reported.
- Source identity, size and modification/change timestamps are checked.
- A second source read checks byte consistency during collection.
- Failed operations retain partial directories; `FAILED.json` is written
  when possible. No successful manifest should be used from a failed run.
- No automatic cleanup deletes a source or retained partial copies.
- New directories use owner-only permissions; files use owner read/write.
- These permissions do not provide encryption or archival immutability.
- Run only in a trusted local storage directory without concurrent writers.
- Symlinked parent directories and hostile filesystem races are not fully
  defended against.
- File fsync is used, but this is not a crash-durable archival protocol.
- A digest verifies byte correspondence, not source authenticity.
- Metadata, ACLs, extended attributes and original timestamps are not copied.
- Copies may contain sensitive material. Keep them and manifests out of Git.

Treat a verified manifest as evidence of byte identity at collection time,
not a complete chain of custody. Reverify hashes before subsequent use.

Binary and empty files can be preserved. Only valid nonblank UTF-8 working
copies within the job input limit can be submitted to text analysis.
A manifest does not itself create an evidence ID.

## Tests

```bash
python3 -m unittest discover -s scripts -p 'test_atlas_*.py' -v
```

Tests use synthetic temporary files only and make no network requests.
