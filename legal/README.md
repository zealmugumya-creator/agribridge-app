# AgriBridge — Legal documents

## Live documents (wired into the app)

Edit only the files in `source/`. They are built into the static site and the API manifest:

| Source | Who must accept | Type |
|---|---|---|
| `source/terms-of-use.md` | Every user | accept |
| `source/privacy-policy.md` | Every user | acknowledge (read) |
| `source/farmer-terms.md` | Role `farmer` | accept |
| `source/buyer-terms.md` | Roles `vendor`, `hotel` | accept |
| `source/supplier-terms.md` | Role `supplier` | accept |

Contact details come from `services/business.py` through `{{TOKENS}}`; never type them into a document.

### Publishing a change

1. Edit the file in `source/`.
2. **If the document was already published, bump `version` in its front matter.** The build refuses to change text under an already-published version (`legal/archive/` holds the frozen copy).
3. Set `material: true` if users must review and accept again, `false` for a correction that need not interrupt anyone. For a material change, fill in `summary` — users see it on the review screen.
4. Run `python scripts/build_legal.py` and commit the generated `static/legal/` and `legal/archive/` files.
5. Run `python scripts/publish_legal.py` (needs the service-role key in the environment) to record the version and hash in the database. The API also does this lazily on first acceptance.

CI runs `python scripts/build_legal.py --check` and fails on stale output.

## Not yet wired

`drafts/` holds the earlier templates (`media-consent.md`, `investor-nda.md`, and the old privacy/terms drafts). They still contain placeholders and are **not** shown to users.

## Review needed

All live documents are unreviewed by a Ugandan advocate. See `docs/LEGAL_PENDING.md` for the open items.
