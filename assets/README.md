# Alert Manager Assets

This directory contains brand assets for Alert Manager including logos, favicons, and social media images.

## Directory Structure

```
assets/
├── logo/               # Logo files for web and print
├── favicon/            # Browser and device icons
└── social/             # Social media sharing images
```

---

## Logo Files (`logo/`)

| Filename | Dimensions | Format | Usage |
|----------|------------|--------|-------|
| `AlertManager.pdn` | - | Paint.NET | Editable source file |
| `AlertManager-master.png` | 1456x816 | PNG | High-res master export |
| `AlertManager-1200x400.png` | 1200x400 | PNG | Web headers, email banners |
| `AlertManager-800x400.png` | 800x400 | PNG | Compact headers, documentation |
| `AlertManager-512x512.png` | 512x512 | PNG | App icon, social avatar, PWA splash |

### Logo Variants

| Variant | Aspect Ratio | Layout | Best For |
|---------|--------------|--------|----------|
| Master | ~16:9 | Stacked | Source/archival |
| 1200x400 | 3:1 | Stacked | Wide headers, banners |
| 800x400 | 2:1 | Stacked | Compact spaces |
| 512x512 | 1:1 | Icon + "AM" | Square contexts |

---

## Favicon Files (`favicon/`)

| Filename | Dimensions | Format | Usage |
|----------|------------|--------|-------|
| `favicon.ico` | 16/32/48 | ICO | Legacy browser support (multi-size) |
| `favicon-16x16.png` | 16x16 | PNG | Browser tab |
| `favicon-32x32.png` | 32x32 | PNG | Browser tab (retina) |
| `favicon-48x48.png` | 48x48 | PNG | Windows shortcuts |
| `apple-touch-icon.png` | 180x180 | PNG | iOS home screen |
| `android-chrome-192x192.png` | 192x192 | PNG | Android home screen |
| `android-chrome-512x512.png` | 512x512 | PNG | Android splash screen, PWA |

---

## Social Media Files (`social/`)

*To be created as needed:*

| Filename | Dimensions | Format | Usage |
|----------|------------|--------|-------|
| `og-image.png` | 1200x630 | PNG | OpenGraph/Facebook sharing |
| `twitter-card.png` | 1200x600 | PNG | Twitter card image |

---

## Brand Colors

The logo uses the **Alternative Palette** from the theme specification:

| Element | Color | Hex |
|---------|-------|-----|
| Background | Deep Navy | `#0F172A` |
| Bell (top) | Blue | `#2563EB` |
| Triangle/Bell (bottom) | Cyan | `#22D3EE` |
| Bar Chart | Dark Navy | `#0F172A` |
| Trend Line | Dark Navy | `#0F172A` |
| "A" and "M" | Amber/Orange | `#F59E0B` |
| "lert" and "anager" | Silver | `#D1D5DB` |

---

## HTML Implementation

Add to your `<head>` section:

```html
<!-- Favicons -->
<link rel="icon" type="image/x-icon" href="/assets/favicon/favicon.ico">
<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/assets/favicon/favicon-16x16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/assets/favicon/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/assets/favicon/android-chrome-192x192.png">

<!-- OpenGraph / Social (when available) -->
<meta property="og:image" content="/assets/social/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="/assets/social/twitter-card.png">
```

---

## Web Manifest (PWA)

For Progressive Web App support, create a `site.webmanifest`:

```json
{
  "name": "Alert Manager",
  "short_name": "AlertMgr",
  "icons": [
    {
      "src": "/assets/favicon/android-chrome-192x192.png",
      "sizes": "192x192",
      "type": "image/png"
    },
    {
      "src": "/assets/favicon/android-chrome-512x512.png",
      "sizes": "512x512",
      "type": "image/png"
    }
  ],
  "theme_color": "#0F172A",
  "background_color": "#0F172A",
  "display": "standalone"
}
```

---

## Usage Guidelines

1. **Minimum Size**: Do not display the full logo smaller than 80px width
2. **Safe Zone**: Maintain 10% padding around the logo
3. **Background**: Use on dark backgrounds (`#0F172A`, `#1e1e1e`, or similar)
4. **Do Not**: Stretch, rotate, add effects, or change colors

---

## Reference

See [docs/THEME_SPECIFICATION.md](../docs/THEME_SPECIFICATION.md) for complete brand guidelines, color palette, and design system documentation.
