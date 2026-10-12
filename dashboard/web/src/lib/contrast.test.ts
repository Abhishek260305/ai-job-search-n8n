// WCAG contrast of the text and background pairs the dashboard uses, in both themes, computed from the real colour
// values: Tailwind's palette (tailwindcss/theme.css) and the app's tokens (src/index.css, `:root` and `.dark`).
// Text needs 4.5:1 (WCAG 2.2 AA, normal size).
import theme from 'tailwindcss/theme.css?raw'
import { describe, expect, it } from 'vitest'
import appCss from '@/index.css?raw'
import { TONE_CLASSES } from '@/lib/status'

type Rgb = [number, number, number]
type Theme = 'light' | 'dark'

function declarations(block: string, into = new Map<string, string>()): Map<string, string> {
  for (const m of block.matchAll(/--([\w-]+):\s*([^;]+);/g)) into.set(m[1], m[2].trim())
  return into
}

const PALETTE = declarations(theme.match(/@theme\s+default\s*\{([\s\S]*?)\n\}/)?.[1] ?? theme)
const ROOT = appCss.match(/:root\s*\{([^}]*)\}/)?.[1] ?? ''
const DARK = appCss.match(/\.dark\s*\{([^}]*)\}/)?.[1] ?? ''
const TOKENS: Record<Theme, Map<string, string>> = {
  light: declarations(ROOT),
  dark: declarations(DARK, declarations(ROOT)),
}

/** oklch(L C H) to gamma-encoded sRGB, each channel 0..1 (clamped to the gamut). */
function oklchToSrgb(value: string): Rgb {
  const m = value.match(/oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+|none)\s*\)/)
  if (!m) throw new Error(`not an oklch colour: ${value}`)
  const L = Number(m[1]) / (m[2] ? 100 : 1)
  const C = Number(m[3])
  const h = m[4] === 'none' ? 0 : (Number(m[4]) * Math.PI) / 180
  const a = C * Math.cos(h)
  const b = C * Math.sin(h)
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const mm = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  const linear: Rgb = [
    4.0767416621 * l - 3.3077115913 * mm + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * mm - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * mm + 1.707614701 * s,
  ]
  return linear.map((c) => {
    const x = Math.min(1, Math.max(0, c))
    return x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055
  }) as Rgb
}

/** A colour by its Tailwind name: "sky-100", "muted-foreground", "foreground/60" (an alpha over `under`). */
function colour(name: string, mode: Theme, under?: Rgb): Rgb {
  const [base, alpha] = name.split('/')
  const value = TOKENS[mode].get(base) ?? PALETTE.get(`color-${base}`)
  if (!value) throw new Error(`unknown colour: ${base}`)
  const rgb = oklchToSrgb(value)
  if (!alpha) return rgb
  const below = under ?? colour('background', mode)
  const t = Number(alpha) / 100
  return rgb.map((c, i) => c * t + below[i] * (1 - t)) as Rgb
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

function contrast(text: string, background: string, mode: Theme): number {
  const bg = colour(background, mode)
  const [hi, lo] = [luminance(colour(text, mode, bg)), luminance(bg)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/** The colour a class list gives `prop` ("bg", "text") in a theme: `dark:` classes win in the dark one. */
function pick(classes: string, prop: 'bg' | 'text', mode: Theme): string {
  const find = (prefix: string) => classes.match(new RegExp(`(?:^|\\s)${prefix}${prop}-([\\w-]+(?:/\\d+)?)`))?.[1]
  const found = (mode === 'dark' ? find('dark:') : undefined) ?? find('')
  if (!found) throw new Error(`no ${prop}- class in "${classes}"`)
  return found
}

describe('the colour maths', () => {
  it('matches known values', () => {
    expect(contrast('foreground', 'background', 'light')).toBeGreaterThan(17)
    // black on white is 21:1; neutral-500 (#737373) on white is 4.74:1
    expect(contrast('neutral-500', 'background', 'light')).toBeCloseTo(4.74, 1)
  })

  it('reads the dark tokens over the light ones', () => {
    expect(contrast('foreground', 'background', 'dark')).toBeGreaterThan(17)
    expect(luminance(colour('background', 'dark'))).toBeLessThan(0.05)
    expect(colour('sky-100', 'dark')).toEqual(colour('sky-100', 'light'))
  })

  it('takes dark: classes in the dark theme only', () => {
    const classes = 'bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200'
    expect([pick(classes, 'bg', 'light'), pick(classes, 'text', 'light')]).toEqual(['sky-100', 'sky-900'])
    expect([pick(classes, 'bg', 'dark'), pick(classes, 'text', 'dark')]).toEqual(['sky-950', 'sky-200'])
    expect(pick('bg-muted text-muted-foreground', 'bg', 'dark')).toBe('muted')
  })
})

// [text classes, background classes, where], as the components write them
const PAIRS: [string, string, string][] = [
  ['text-muted-foreground', 'bg-background', 'hints, captions, the header time'],
  ['text-muted-foreground', 'bg-card', 'card descriptions, the funnel labels'],
  ['text-muted-foreground', 'bg-muted', 'a selected row, the pressed card, the tab bar'],
  ['text-muted-foreground', 'bg-red-50 dark:bg-red-950', "a failed gate's note"],
  ['text-muted-foreground', 'bg-amber-50 dark:bg-amber-950', 'the safety box'],
  ['text-muted-foreground', 'bg-destructive/5', 'the error boxes'],
  ['text-foreground/60 dark:text-muted-foreground', 'bg-muted', 'an inactive tab'],
  ['text-amber-900 dark:text-amber-200', 'bg-background', 'the attention reason'],
  ['text-destructive', 'bg-card', 'the login error'],
  ['text-primary', 'bg-background', 'links'],
  ['text-primary-foreground', 'bg-primary', 'primary buttons'],
  ['text-secondary-foreground', 'bg-secondary', 'secondary badges'],
  ['text-popover-foreground', 'bg-popover', 'menus, toasts'],
  ['text-foreground', 'bg-card', 'card text, the funnel counts, the donut total'],
]

for (const mode of ['light', 'dark'] as const) {
  describe(`${mode} theme`, () => {
    describe('status badges', () => {
      for (const [tone, classes] of Object.entries(TONE_CLASSES)) {
        it(`${tone}: ${classes}`, () => {
          expect(contrast(pick(classes, 'text', mode), pick(classes, 'bg', mode), mode)).toBeGreaterThanOrEqual(4.5)
        })
      }
    })

    describe('other text', () => {
      for (const [text, bg, where] of PAIRS) {
        it(`${text} on ${bg} (${where})`, () => {
          expect(contrast(pick(text, 'text', mode), pick(bg, 'bg', mode), mode)).toBeGreaterThanOrEqual(4.5)
        })
      }
    })
  })
}
