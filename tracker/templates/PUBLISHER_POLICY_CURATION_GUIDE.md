# Publisher repository-policy inventory

The publisher inventory is maintained in two pinned reference tables:

- `reference/publisher_policy_sources.csv`: authoritative source pages and response hashes.
- `reference/publisher_repository_mentions.csv`: human-coded repository mentions.

Use these controlled mention types:

- `mandatory`: the publisher or journal requires deposition for a stated data type.
- `recommended`: the resource is explicitly recommended.
- `preferred`: the policy prefers the resource or a named class containing it.
- `acceptable_fallback`: the resource is identified as an acceptable generalist or fallback option.
- `example`: the resource is illustrative, not necessarily recommended.
- `directory_referral`: the policy points authors to a directory such as FAIRsharing or re3data rather than endorsing a specific repository.

The public `publisher_recommended` flag is true only for `mandatory`, `recommended`, or `preferred`. It is false for examples, acceptable fallbacks, and directory referrals, although those mentions remain displayed.

`step_update_publisher_info.py` refreshes and hashes source pages. It never converts changed web content directly into new card assertions. When a source changes or expected anchor terms disappear, review and revise the mention table manually, then freeze the revised table and source metadata.
