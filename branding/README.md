# Branding (RHIS house style)

This folder holds this project's copy of the RHIS house style, taken from
[`rhisijmuiden/rhis-huisstijl`](https://github.com/rhisijmuiden/rhis-huisstijl)
at commit `7e4805f`. That repo is the source: change the style there first,
then copy the changed files here.

| Path | What |
|------|------|
| `huisstijlgids.md` | The style guide (logo rules, colors, type, tone) |
| `tokens/tokens.css`, `tokens/tokens.json` | Design tokens, light and dark |
| `logo/` | Wordmark (light/dark), app icon, antenna, favicons |
| `fonts/` | Archivo variable webfont, self-hosted |

The app serves this folder at `/brand`. `app/static/css/shell.css` maps every
tool's own color variables onto these tokens and draws the left menu
(`app/templates/shared/_sidebar.html`).
