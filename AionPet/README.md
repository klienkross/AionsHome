# Aion Animation Pack

This folder contains generic app-ready Aion sprite animations, including the original Codex-compatible base actions and extra companion/emotion actions.

## Copy This Folder

Copy the whole folder:

```text
aion-emotions-v1/
```

Minimum files needed by another project:

```text
manifest.json
webp/
strips/
frames/
```

`source/` and `qa/` are kept for review and debugging. They are not required at runtime.

## Sprite Format

- Format: horizontal sprite strips
- Cell size: 192 x 208 px
- Anchor: bottom-center
- Alpha: transparent RGBA
- Frame x position: `frameIndex * 192`
- Frame y position: `0`

## Animations

| ID | Frames | Duration | Loop | Notes |
| --- | ---: | ---: | --- | --- |
| `idle` | 6 | variable | yes | Breathing and blinking idle. |
| `running-right` | 8 | variable | yes | Runs to the right. |
| `running-left` | 8 | variable | yes | Runs to the left. |
| `waving` | 4 | variable | yes | Waves hello. |
| `jumping` | 5 | variable | yes | Small jump. |
| `failed` | 8 | variable | yes | Failure/sad reaction. |
| `waiting` | 6 | variable | yes | Waiting loop. |
| `running` | 6 | variable | yes | Runs in place. |
| `review` | 6 | variable | yes | Focused review/thinking loop. |
| `happy` | 6 | 140 ms | yes | Smiles and bounces lightly. |
| `angry` | 6 | 140 ms | yes | Frowns and clenches fists. |
| `tsundere` | 6 | 150 ms | yes | Looks away, arms crossed, glances back. |
| `sleepy` | 6 | 180 ms | yes | Drowsy standing loop with yawn. |
| `sleep_prone` | 8 | 180 ms | yes | Frames 0-2 lie-down transition; loop from frame 3. |

For actions marked `variable`, read `frameDurationsMs` in `manifest.json`.

## Runtime Notes

Prefer `webp/*.webp` when the target renderer supports transparent WebP.

Use `strips/*.png` as a universal fallback.

Use `frames/<animation-id>/*.png` when the target runtime prefers individual frame files.
