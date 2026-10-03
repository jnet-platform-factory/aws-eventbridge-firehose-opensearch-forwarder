# Example ShapingConfig field maps

Each file is a valid `ShapingConfig` stack parameter, one per shape the
interpreter in `app/src/shaping.py` supports. The tests load them; they are also
the quickest starting point for your own map.

| File | What it shows |
|---|---|
| `promote-and-nest.json` | an `int` promotion plus a nested user-context object |
| `nest-only.json` | the nested object with no promotion |
| `application-fallback.json` | a deployment-specific fallback for `application`, nothing else |

Minify one into a stack parameter with:

    jq -c . tests/fixtures/shaping/promote-and-nest.json
