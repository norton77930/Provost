## Problem and scope

What does this change address? List important non-goals.

## Behaviour for existing installs

What changes for someone who runs `install.py install` after this? Does any hook,
record format, or installer guarantee (fail-soft hooks, record integrity,
backups and rollback) become weaker? State explicitly if nothing changes.

## How to revert

How does someone undo this after installing it? For example reverting the merge
and running `install.py install` again from that checkout, or restoring files by
hand from the backup directory (`install.py` has no rollback command). State it
plainly when reverting the merge is enough.

## Verification

List commands run and current results. Note checks that could not be run.

## Documentation and attribution

- [ ] Status/claims remain consistent across relevant documentation.
- [ ] No credential, private path, or private repository content is included.
- [ ] Third-party attribution is preserved.
