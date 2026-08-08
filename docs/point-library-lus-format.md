# Lumeri Point Library `.lus` v2

This document extends [`lus-skill-format.md`](lus-skill-format.md). The legacy
`#!lus/1` text Skill remains unchanged. A Point Library is a deterministic ZIP
bundle whose filename ends in `.lus`; its `manifest.lus` is still a valid
`#!lus/1` document so existing parsers can inspect the Skill guidance.

## Bundle contract

Every bundle contains these UTF-8 members:

```text
manifest.lus
catalog.json
verification.json
runtime/manifest.json       # Shape A native binding
ops/manifest.json            # Shape B declarative binding
```

The last two are alternatives. A native binding names an already installed
Lumeri dispatcher. An ops binding names existing safe Lumeri tools and their
semantic argument mapping. Lumeri never imports or executes code from a
bundle, and the cloud service never executes either binding.

The manifest declares `kind: point_library` and a `point_library` object:

```yaml
kind: point_library
point_library:
  shape: A                 # A or B
  category: SYNTHESIS      # SYNTHESIS or TRANSFORM
  implementation:
    mode: builtin           # builtin or ops
    entrypoint: vector_motion
  output:
    artifact: object
    next: string
    errors:
      E_ARG: {recoverable: true}
      E_RENDER: {recoverable: true}
```

`parameters` uses the existing `.lus` JSON-Schema subset. Model-facing fields
are semantic names such as `brief`, `feedback`, or `target_asset_id`; defaults
are declared on those properties. Craft numbers, coordinates, easing values,
tone curves, and other internal mappings belong only to the native
implementation or safe ops binding.

`manifest.lus` body is the Skill guidance: when to call the library, how to
compose its catalog entries, boundaries, and recovery behavior. It is
advisory; the executable contract is the validated binding and catalog.

## Catalog and verification

`catalog.json` contains a closed, unique, kebab-case `entries` list. Each entry
has an addressable `id` and human-readable `title`; additional vocabulary is
library-specific and must remain semantic.

`verification.json` must contain:

```json
{
  "deterministic": true,
  "taste_floor": ["library-specific structural invariant"],
  "tests": ["determinism fixture", "taste-floor fixture"]
}
```

The local installer validates the package structure, safe paths, member limits,
manifest contract, catalog, and verification record before writing anything.
It stages bytes, atomically activates the exact `id/version`, and removes a
newly staged package if activation fails. Installing the same version with
different bytes is a conflict. Older installed versions remain available for
exact rollback.

## Agent surfaces

Shape A exposes one `point_library` host tool with `op: create|adjust|catalog`.
Shape B exposes the same host dispatcher as `op: apply|describe`; it does not
create a Tool for each catalog entry. Successful calls return a structured
artifact/result and a `next` verification pointer. Recoverable failures use a
typed `error_code` and `recoverable: true`.

## Skill Cloud

Cloud upload accepts `kind: point_library`, preserves the original base64
bundle bytes, normalized metadata, version, and content hash, and validates the
ZIP without executing it. Artifacts are private by default. A public artifact
requires explicit `public_confirmed: true`; only then does it enter the public
catalog. Local Lumeri explicitly installs or rolls back a downloaded exact
version.
