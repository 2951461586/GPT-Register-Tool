---
name: linear-ui-skills
description: Linear's UI design system. Use when building interfaces inspired by Linear's aesthetic - dark mode, Inter font, 4px grid.
license: MIT
metadata:
  author: design-skills
  version: "1.0.0"
  source: https://linear.app
---

# Linear UI Skills

Opinionated constraints for building Linear-style interfaces with AI agents.

## When to Apply

Reference these guidelines when:
- Building dark-mode interfaces
- Creating Linear-inspired design systems
- Implementing UIs with Inter font and 4px grid

## Colors

- MUST use dark backgrounds (lightness < 20) for primary surfaces - detected lightness: 4
- MUST use `#080A0A` as page background (`surface-base`)
- SHOULD limit color palette to 10 distinct colors
- MUST maintain text contrast ratio of at least 4.5:1 for accessibility

### Semantic Tokens

| Token | HEX | RGB | Usage |
|-------|-----|-----|-------|
| `surface-base` | #080A0A | rgb(8,10,10) | Page background |
| `surface-raised` | #D2D2D3 | rgb(210,210,211) | Cards, modals, raised surfaces |
| `surface-overlay` | #E3E3E6 | rgb(227,227,230) | Overlays, tooltips, dropdowns |
| `text-primary` | #5C5C5C | rgb(92,92,92) | Headings, body text |
| `text-secondary` | #2D2E30 | rgb(45,46,48) | Secondary, muted text |
| `text-tertiary` | #444749 | rgb(68,71,73) | Additional text |
| `border-default` | #B0B1B1 | rgb(176,177,177) | Subtle borders, dividers |

## Typography

- MUST use `Inter` as primary font family
- SHOULD use single font family for consistency
- MUST use `60px` / `700` for primary headings
- MUST use `17px` / `400` for body text
- SHOULD reduce font weights (currently 4 detected)
- MUST use `text-balance` for headings and `text-pretty` for body text
- SHOULD use `tabular-nums` for numeric data
- NEVER modify letter-spacing unless explicitly requested

### Text Styles

| Style | Font | Size | Weight | Color | Count |
|-------|------|------|--------|-------|-------|
| `heading-1` | Inter | 60px | 700 | #E2E4E3 | 1 |
| `body` | Inter | 17px | 400 | #444749 | 1 |
| `body-secondary` | Inter | 16px | 400 | #5C5C5C | 1 |
| `body-secondary` | Inter | 15px | 500 | #2D2E30 | 1 |
| `text-14px` | Inter | 14px | 400 |

