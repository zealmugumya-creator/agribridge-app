# Legal: what is still open

Everything here needs the owner or a Ugandan advocate. None of it was invented. The published documents say "to be confirmed" where a detail is missing.

## Details the owner has not supplied (never invented)

| Item | Where it is needed |
|---|---|
| Registered legal name of the operator | Privacy Policy s.2, Terms footer |
| Company / business registration number | Privacy Policy s.2 |
| Physical address | Privacy Policy s.2, Terms s.24 |
| Name of a data protection / privacy officer | Privacy Policy s.2 (the old in-app text named a person; that was removed) |
| PDPO registration number, if registered | Privacy Policy s.2, s.23 |
| Effective date to publish (currently 2026-10-09, set at build time) | front matter of each file in `legal/source/` |

To add them, edit the source files, bump the version, set `material`, rebuild (see `legal/README.md`).

## For a Ugandan advocate to confirm

1. Data Protection and Privacy Act, 2019 and Regulations 2021: whether AgriBridge must register with the PDPO, and the correct complaint route and wording in Privacy Policy s.23.
2. Time limit for answering access / correction / deletion requests (Privacy Policy s.17 says "as the law allows").
3. Retention periods for orders, payments and consent records (Privacy Policy s.16).
4. Whether keeping consent/acceptance evidence after a hard account purge is acceptable (the table has no foreign key to `auth.users` on purpose).
5. Cross-border transfer wording and the actual hosting regions of Supabase, Render and Cloudflare (s.14).
6. Consumer-protection rights that apply to online marketplace sales, and the refund wording in Terms s.10 and s.12.
7. Whether any limit on liability, a governing court or a dispute forum (Terms s.20-22) should be added. This version deliberately has no liability cap, no mandatory arbitration and no blanket refund exclusion.
8. Fees and commission: Terms s.10 only says fees are shown before you confirm. The old in-app text said 2.5% / 5-8%; that was not carried over because it could not be verified in the code.
9. Whether AgriBridge itself holds funds (escrow-like behaviour). The old text promised escrow above UGX 500,000; nothing in the code does this, so the new Terms say AgriBridge is not an escrow provider.
10. Farmer payout conditions and timing (Farmer Terms s.8) and supplier verification standard (Supplier Terms s.1).
11. Regulated inputs: which Ugandan approvals suppliers of agrochemicals and veterinary products must hold (Supplier Terms s.3).
12. The media/photo licence in Terms s.13 and whether a separate media consent is needed (`legal/drafts/media-consent.md` is not wired).
13. Whether business buyers need a separate agreement for invoicing / credit terms. The old text promised 30-day terms and a 2% monthly penalty; both were removed because nothing implements them.

## Claims in the app that are not verified and that I did not change

These are outside the legal flow, but they are statements to users. Please confirm or remove them:

- "5,000+ farmers connected" and "Uganda's #1 Farm-to-Table Platform" (USSD About screen in `app.py`).
- "Farm-to-door within 24hrs" (USSD bulk orders screen).
- "Emergency vet: 24/7" was removed together with the opening-hours lines, which disagreed with each other (7am-8pm vs 7am-9pm).
- "Kampala, Uganda" is shown as the office on the Contact page. No street address was added.

## Delivery partners

The app has no delivery-partner sign-up, so there is no delivery-partner agreement and no such role. If one is added, add `delivery-terms.md`, a role in `scripts/build_legal.py` (`ROLES`) and `services/legal.normalize_role`, and a sign-up option.
