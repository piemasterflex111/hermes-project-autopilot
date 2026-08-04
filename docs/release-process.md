# Release process

1. Merge compatibility and evidence changes only after repository integrity and replay workflows pass.
2. Create an SSH-signed annotated tag.
3. Build the versioned archive, SPDX 2.3 SBOM, manifest, checksums, and detached SSH signature.
4. Verify checksums and signature locally.
5. Publish all artifacts as GitHub Release assets.
6. Preserve each upstream compatibility series in a versioned directory; never replace historical provenance.

```bash
git -c gpg.format=ssh -c user.signingkey=$HOME/.ssh/id_ed25519 tag -s v1.1.0 -m 'Hermes Project Autopilot v1.1.0'
python3 scripts/build_release_bundle.py \
  --repo . --tag v1.1.0 --output-dir dist/v1.1.0 \
  --signing-key $HOME/.ssh/id_ed25519
```
