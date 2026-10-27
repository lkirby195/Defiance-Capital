# Brand palette

Pulled from https://defiancecapitalco.com on 2026-10-26 (a Squarespace 7.1 site, served from
https://www.defiancecapitalco.com). The site's palette lives in its versioned stylesheet
(`static1.squarespace.com/static/versioned-site-css/696e68cbf9458b73f3ddf328/.../site.css`,
referred to below as site.css) as HSL custom properties that every section theme draws
on; the logo is a PNG the header serves from the Squarespace CDN. **No gold tone exists**
anywhere on the site or in the logo; the only warm value in the page is Squarespace's
own form-error red.

## Palette

| Role | Hex | Where it was found |
|---|---|---|
| Primary (navy) | `#3f5773` | site.css `--darkAccent-hsl: 213,29%,35%`, the background of the site's "dark" section theme. The logo's navy is `#3c5473` / `#3f5673` (53% of its opaque pixels), the same tone. |
| Primary, deep | `#202b37` | site.css `--accent-hsl: 211,27%,17%` (also `--safeDarkAccent-hsl`): the primary button background and the hero's "black" theme. The text colour here. |
| Accent (blue) | `#2c84cd` | the logo's blue wedge (1.4% of its opaque pixels). The stylesheet fills buttons and colours links with `#2676b8`, one step darker, so white button text and the link on the page both clear 4.5:1. |
| Accent, light (sky) | `#73ccf4` | the logo's sky-blue D (39% of its opaque pixels). Focus rings and chip borders. |
| Green | `#109e00` | the logo's two green wedges (6% of its opaque pixels). Not used by the stylesheet: the verdict and severity greens stay semantic. |
| Background, light | `#edf0f2` | site.css `--lightAccent-hsl: 204,17%,94%`, the background of the "light" section theme. The page background here. |
| White | `#fafaf9` | site.css `--white-hsl: 60,9%,98%` (also `--safeLightAccent-hsl` and `--safeInverseAccent-hsl`): the "white" sections and the solid header (`--solidHeaderBackgroundColor`). Cards and the top bar here. |
| Black | `#000000` | site.css `--black-hsl: 0,0%,0%`: body text on the light themes. |

Derived here, not on the site: `--primary-tint #e3e9f0` (the navy at about a tenth on
white: the table subheader rows), `--accent-tint #e6f4fc` (the sky blue at a tenth on
white: chips), `--accent-hover #1f679f`, and the neutrals `--muted #5b6472`, `--line
#dfe3e8`, `--line-strong #c5ccd5`, `--stripe #f4f6f8`, `--well #f4f6f8`.

Hex values Squarespace's framework CSS carries that are not the brand and were ignored:
`#f0523d` (form error red), `#00b2ff`, `#272727`, `#3e3e3e`, `#f6f6f6`, `#e7e7e7`
(editor chrome and defaults).

## Logo

- `static/brand/logo.png`: the header logo as served, `LOGO.png?format=original`, a
  2387 x 2357 RGBA PNG from
  `https://images.squarespace-cdn.com/content/v1/696e68cbf9458b73f3ddf328/c3dc6da2-00a2-4b2d-be4d-f73c46b4c5ca/LOGO.png`.
  A stacked mark: the D in sky blue, navy, green and blue over DEFIANCE CAPITAL in the
  navy. The CDN answers with WebP unless the request's Accept header refuses it.
- `static/brand/logo-bar.png`: the same pixels re-laid for the 52px top bar - the D and
  the wordmark cropped out of logo.png and set side by side on a transparent background,
  exported at 3x (424 x 120, shown 40px tall). No SVG exists on the site; its only SVGs are
  Squarespace's social icons.
- Favicon, not copied:
  `https://images.squarespace-cdn.com/content/v1/696e68cbf9458b73f3ddf328/0bad2f26-ecd8-4162-9f66-fd742d0bae94/favicon.ico`.

## Where the stylesheet uses it

`api/templates/_style.html`, `:root`: section title bars `--primary`, table subheaders
`--primary-tint`, buttons and links `--accent`, chips `--accent-tint` on `--accent-soft`,
the page `--bg`, cards and the top bar `--panel`, text `--ink`. The verdict and severity
greens, ambers and reds are semantic and unchanged. `api/main.py` mounts `static/` at
`/static`, which is how both top bars reach the logo.
