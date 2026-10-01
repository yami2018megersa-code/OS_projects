# Short Order sprite set

Forty-one 32 x 32 transparent PNGs: the ingredients and props the kitchen is made
of, and the badges a scheduler earns.

The complete set was generated with the built-in image-generation tool in the
tactile miniature-3D style of the presentation visuals. [`PROMPTS.md`](PROMPTS.md)
records the shared style, every per-asset subject, and the reduction procedure.
The high-resolution originals remain in Codex's generated-image store; the
project contains the optimised runtime sprites.

| Group | Count | What they are for |
| --- | ---: | --- |
| Ingredients | 16 | At least one per recipe on the course menu, for ticket cards and the larder |
| Props | 7 | The board, knife, pan, plate, tray, ticket and crate the kitchen works with |
| Beat-a-baseline badges | 11 | One per reference scheduler in the engine |
| Best-by-metric badges | 7 | The six score components, plus the total |

Every delivered file is a 32 x 32 RGBA PNG with transparent corners, no
background plate and no baked-in text, so the interface can put its own labels
next to it. The badges use a common centred shield family; rim colour identifies
the tier and the device identifies the achievement.
