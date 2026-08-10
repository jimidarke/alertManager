# Alert Manager Theme Specification

**Version:** 1.0.1
**Last Updated:** 2025-12-01

This document defines the official UI/UX design system for Alert Manager, ensuring visual consistency across all interfaces including the HTTP Gateway, email templates, and documentation.

---

## Table of Contents

1. [Design Philosophy](#design-philosophy)
2. [Color System](#color-system)
3. [Typography](#typography)
4. [Alert Level Colors](#alert-level-colors)
5. [Component Styles](#component-styles)
6. [Logo Specifications](#logo-specifications)
7. [Platform-Specific Guidelines](#platform-specific-guidelines)
8. [CSS Variables Reference](#css-variables-reference)

---

## Design Philosophy

Alert Manager follows a **dark-mode-first** design approach inspired by modern IDE interfaces. The design prioritizes:

- **Readability**: High contrast text on dark backgrounds reduces eye strain during monitoring
- **Scanability**: Alert levels are instantly recognizable through consistent color coding
- **Professionalism**: Clean, minimal aesthetics suitable for enterprise environments
- **Accessibility**: WCAG 2.1 AA compliant contrast ratios

---

## Color System

### Primary Palette (Dark Mode - Default)

| Role | Color | Hex | RGB | Usage |
|------|-------|-----|-----|-------|
| **Background Primary** | Charcoal | `#1e1e1e` | 30, 30, 30 | Main page/app background |
| **Background Secondary** | Dark Gray | `#252526` | 37, 37, 38 | Cards, headers, panels |
| **Background Tertiary** | Medium Gray | `#2d2d2d` | 45, 45, 45 | Hover states, dividers |
| **Surface** | Control Gray | `#3c3c3c` | 60, 60, 60 | Buttons, inputs, controls |
| **Border** | Border Gray | `#555555` | 85, 85, 85 | Borders, separators |

### Accent Colors

| Role | Color | Hex | RGB | Usage |
|------|-------|-----|-----|-------|
| **Accent Primary** | Alert Blue | `#007acc` | 0, 122, 204 | Primary actions, links, highlights |
| **Accent Hover** | Light Blue | `#1a8ad4` | 26, 138, 212 | Hover state for accent |
| **Accent Focus** | Bright Blue | `#3794ff` | 55, 148, 255 | Focus rings, active states |

### Typography Colors

| Role | Color | Hex | RGB | Usage |
|------|-------|-----|-----|-------|
| **Text Primary** | Light Gray | `#d4d4d4` | 212, 212, 212 | Body text, primary content |
| **Text Secondary** | Muted Gray | `#858585` | 133, 133, 133 | Labels, timestamps, metadata |
| **Text Title** | White | `#ffffff` | 255, 255, 255 | Page titles, section headers |
| **Text Header** | Accent Blue | `#007acc` | 0, 122, 204 | H1 headings, emphasized headers |
| **Text Value** | Teal | `#4ec9b0` | 78, 201, 176 | Numeric values, highlighted data |

### Semantic Colors

| Role | Color | Hex | Usage |
|------|-------|-----|-------|
| **Success** | Green | `#0e8a16` | Connected status, success states |
| **Error** | Red | `#d73a49` | Disconnected status, error states |
| **Link** | Blue | `#3794ff` | Hyperlinks |
| **Link Visited** | Purple | `#9e6fff` | Visited hyperlinks |

---

## Typography

### Font Stack

```css
/* Monospace (Primary - Log Viewer, Code) */
font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;

/* Sans-serif (Secondary - UI Labels, Buttons) */
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;

/* Email Templates */
font-family: Arial, Verdana, Helvetica, sans-serif;
```

### Type Scale

| Element | Size | Weight | Color | Line Height |
|---------|------|--------|-------|-------------|
| **Page Title (H1)** | 24px | 400 | `#007acc` | 1.2 |
| **Section Header (H2)** | 18px | 600 | `#ffffff` | 1.3 |
| **Card Title (H3)** | 16px | 600 | `#d4d4d4` | 1.4 |
| **Body Text** | 14px | 400 | `#d4d4d4` | 1.6 |
| **Small/Caption** | 12px | 400 | `#858585` | 1.5 |
| **Tiny/Version** | 11px | 400 | `#858585` | 1.4 |
| **Log Content** | 13px | 400 | (varies by level) | 1.6 |

---

## Alert Level Colors

These colors are used consistently across ALL interfaces (log viewer, email reports, LED status, documentation).

### Foreground Colors (Text/Icons)

| Level | Color Name | Hex | RGB | Preview |
|-------|------------|-----|-----|---------|
| **Critical** | Coral Red | `#f48771` | 244, 135, 113 | High urgency |
| **Warning** | Amber Yellow | `#dcdcaa` | 220, 220, 170 | Caution |
| **Notice** | Teal | `#4ec9b0` | 78, 201, 176 | Attention |
| **Info** | Sky Blue | `#9cdcfe` | 156, 220, 254 | Informational |
| **Debug** | Forest Green | `#6a9955` | 106, 153, 85 | Technical |

### Background Colors (Badges/Highlights)

| Level | Background Hex | Border Hex | Usage |
|-------|----------------|------------|-------|
| **Critical** | `#5a1a1a` | `#f48771` | Alert badges, row highlights |
| **Warning** | `#4d4d1a` | `#dcdcaa` | Warning banners |
| **Notice** | `#1a4d4d` | `#4ec9b0` | Notice indicators |
| **Info** | `#1a3d4d` | `#9cdcfe` | Info badges |
| **Debug** | `#1a3d1a` | `#6a9955` | Debug indicators |

### Alert Level CSS Classes

```css
/* Text colors */
.level-critical { color: #f48771; }
.level-warning  { color: #dcdcaa; }
.level-notice   { color: #4ec9b0; }
.level-info     { color: #9cdcfe; }
.level-debug    { color: #6a9955; }

/* Badge/Pill styles */
.badge-critical { background: #5a1a1a; color: #f48771; }
.badge-warning  { background: #4d4d1a; color: #dcdcaa; }
.badge-notice   { background: #1a4d4d; color: #4ec9b0; }
.badge-info     { background: #1a3d4d; color: #9cdcfe; }
.badge-debug    { background: #1a3d1a; color: #6a9955; }
```

---

## Component Styles

### Buttons

```css
/* Primary Button */
.btn-primary {
    background: #007acc;
    color: #ffffff;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 14px;
    cursor: pointer;
    transition: background 0.2s;
}
.btn-primary:hover { background: #1a8ad4; }
.btn-primary:active { background: #005a9e; }

/* Secondary Button */
.btn-secondary {
    background: #3c3c3c;
    color: #d4d4d4;
    border: 1px solid #555555;
    padding: 8px 16px;
    border-radius: 4px;
}
.btn-secondary:hover { background: #464646; }
```

### Cards/Panels

```css
.card {
    background: #252526;
    border-radius: 8px;
    padding: 20px;
    border-left: 4px solid #007acc;
}
```

### Form Controls

```css
input, select, textarea {
    background: #3c3c3c;
    color: #d4d4d4;
    border: 1px solid #555555;
    padding: 8px 12px;
    border-radius: 4px;
    font-size: 14px;
}
input:focus, select:focus, textarea:focus {
    border-color: #007acc;
    outline: none;
    box-shadow: 0 0 0 2px rgba(0, 122, 204, 0.25);
}
```

### Status Badges

```css
.status-connected    { background: #0e8a16; color: #ffffff; }
.status-disconnected { background: #d73a49; color: #ffffff; }
.status-pending      { background: #dcdcaa; color: #1e1e1e; }
```

### Scrollbars (WebKit)

```css
::-webkit-scrollbar { width: 10px; }
::-webkit-scrollbar-track { background: #1e1e1e; }
::-webkit-scrollbar-thumb { background: #3c3c3c; border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: #555555; }
```

---

## Logo Specifications

### Required Logo Variants

Create the logo in the following formats and sizes:

#### 1. Vector Source (Required)
| Format | Usage |
|--------|-------|
| **SVG** | Web display, scalable, preferred format |
| **AI/EPS** | Print, high-quality exports |

#### 2. Favicon Package

| Size | Filename | Usage |
|------|----------|-------|
| **16x16** | `favicon-16x16.png` | Browser tab (standard) |
| **32x32** | `favicon-32x32.png` | Browser tab (retina) |
| **48x48** | `favicon-48x48.png` | Windows site shortcuts |
| **ICO** | `favicon.ico` | Legacy browsers (contains 16, 32, 48) |

#### 3. Apple/iOS Icons

| Size | Filename | Usage |
|------|----------|-------|
| **180x180** | `apple-touch-icon.png` | iOS home screen |
| **152x152** | `apple-touch-icon-152x152.png` | iPad |
| **120x120** | `apple-touch-icon-120x120.png` | iPhone retina |

#### 4. Android/PWA Icons

| Size | Filename | Usage |
|------|----------|-------|
| **192x192** | `android-chrome-192x192.png` | Android home screen |
| **512x512** | `android-chrome-512x512.png` | Android splash screen, PWA |

#### 5. Web Display

| Size | Filename | Usage |
|------|----------|-------|
| **SVG** | `logo.svg` | Primary web logo (scalable) |
| **200x60** | `logo-header.png` | HTTP Gateway header |
| **120x120** | `logo-square.png` | Square contexts |
| **300x90** | `logo-email.png` | Email template header |

#### 6. Social/OpenGraph

| Size | Filename | Usage |
|------|----------|-------|
| **1200x630** | `og-image.png` | Social media sharing |
| **1200x600** | `twitter-card.png` | Twitter cards |

### Logo Color Variants

| Variant | Background | Logo Color | Usage |
|---------|------------|------------|-------|
| **Primary** | Transparent | `#007acc` (Blue) | Dark backgrounds |
| **Inverted** | Transparent | `#ffffff` (White) | Dark headers |
| **Dark** | Transparent | `#1e1e1e` (Charcoal) | Light backgrounds (email) |
| **On-Dark** | `#1e1e1e` | `#007acc` | Social media, thumbnails |

### Alternative Logo Palette (Special Projects)

An alternative color palette for the logo, suitable for special projects, marketing materials, or when a more modern/enterprise aesthetic is desired. This palette emphasizes visual separation between "alert" (urgency) and "analytics" (data/insight) concepts.

#### Alternative Palette Colors

| Role | Color Name | Hex | RGB | Usage |
|------|------------|-----|-----|-------|
| **Background** | Deep Navy | `#0B1020` | 11, 16, 32 | Logo background, dark contexts |
| **Background Alt** | Slate | `#111827` | 17, 24, 39 | Alternative dark background |
| **Alert Accent** | Amber | `#F97316` | 249, 115, 22 | Alert icons, warning elements |
| **Alert Accent Dark** | Dark Amber | `#EA580C` | 234, 88, 12 | Hover/pressed states |
| **Analytics Accent** | Cyan | `#22D3EE` | 34, 211, 238 | Charts, data visualization |
| **Analytics Alt** | Teal | `#06B6D4` | 6, 182, 212 | Secondary data elements |
| **Neutral Light** | Silver | `#E5E7EB` | 229, 231, 235 | Secondary text, subtle elements |
| **Neutral Mid** | Gray | `#9CA3AF` | 156, 163, 175 | Muted text, borders |

#### Logo Element Mapping

| Element | Primary Palette | Alternative Palette |
|---------|-----------------|---------------------|
| Icon background | `#1e1e1e` Charcoal | `#0B1020` Deep Navy |
| Alert triangle/bell | `#007acc` Blue | `#F97316` Amber |
| Chart/pulse/bars | `#4ec9b0` Teal | `#22D3EE` Cyan |
| "Alert" text | `#007acc` Blue | `#F97316` Amber |
| "Manager" text | `#d4d4d4` Light Gray | `#E5E7EB` Silver |

#### Typography Recommendations (Logo)

For the wordmark portion of the logo:

| Font Style | Recommended Fonts | Notes |
|------------|-------------------|-------|
| **Geometric Sans** | Inter, Poppins, Montserrat, Roboto | Clean, technical feel |
| **"Alert" weight** | Semi-bold (600) or Bold (700) | Emphasis on urgency |
| **"Manager" weight** | Regular (400) or Medium (500) | Balanced, professional |

**Design Notes:**
- Keep complexity in the icon, not the wordmark
- Avoid over-stylized or decorative fonts
- Ensure legibility at small sizes (favicon)
- The amber/cyan combination provides strong visual contrast while avoiding the "panic red" aesthetic

#### Alternative Palette Use Cases

- Marketing and promotional materials
- Special project branding (e.g., "Alert Manager Enterprise")
- Conference/presentation slides
- Merchandise and swag
- Partner co-branding situations
- When differentiation from the standard UI theme is desired

#### File Naming Convention

Alternative palette assets should use the `-alt` suffix:

```
/assets/logo/
├── logo.svg                    # Primary palette
├── logo-alt.svg                # Alternative palette
├── logo-header.png             # Primary, 200x60
├── logo-header-alt.png         # Alternative, 200x60
└── ...
```

### Logo Safe Zone

- Maintain minimum padding of **10%** of logo width on all sides
- Never stretch or distort the logo
- Minimum display size: **80px width** for legibility

### File Organization

```
/assets/
├── logo/
│   ├── logo.svg                    # Vector source
│   ├── logo-header.png             # 200x60, web header
│   ├── logo-email.png              # 300x90, email templates
│   ├── logo-square.png             # 120x120, square contexts
│   └── logo-dark.svg               # Dark variant for light backgrounds
├── favicon/
│   ├── favicon.ico                 # Multi-size ICO
│   ├── favicon-16x16.png
│   ├── favicon-32x32.png
│   ├── favicon-48x48.png
│   ├── apple-touch-icon.png        # 180x180
│   ├── android-chrome-192x192.png
│   └── android-chrome-512x512.png
└── social/
    ├── og-image.png                # 1200x630
    └── twitter-card.png            # 1200x600
```

---

## Platform-Specific Guidelines

### HTTP Gateway (Dark Mode)

The HTTP Gateway log viewer uses the full dark theme:

```css
body {
    background: #1e1e1e;
    color: #d4d4d4;
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}
```

- Headers: `#252526` background with `#007acc` accent border
- Controls: `#3c3c3c` background
- Log container: `#1e1e1e` with `#3c3c3c` border

### Email Templates (Light Mode)

Email templates use a **light theme** for maximum email client compatibility:

| Element | Color | Hex |
|---------|-------|-----|
| **Wrapper Background** | Light Gray | `#f5f5f5` |
| **Container Background** | White | `#ffffff` |
| **Header Border** | Accent Blue | `#007acc` |
| **Header Title** | Accent Blue | `#007acc` |
| **Header Subtitle** | Gray | `#666666` |
| **Body Text** | Dark Gray | `#333333` |
| **Secondary Text** | Gray | `#666666` |
| **Footer Background** | Off-White | `#f9f9f9` |
| **Footer Text** | Muted | `#888888` |
| **Table Header** | Accent Blue | `#007acc` |
| **Table Header Text** | White | `#ffffff` |
| **Table Rows Odd** | White | `#ffffff` |
| **Table Rows Even** | Off-White | `#f9f9f9` |
| **Table Border** | Light Gray | `#eeeeee` |

#### Email Alert Level Colors (Light Background)

| Level | Text/Border | Background |
|-------|-------------|------------|
| **Critical** | `#c0392b` | `#fdf2f2` |
| **Warning** | `#d68910` | `#fef9e7` |
| **Notice** | `#148f77` | `#e8f8f5` |
| **Info** | `#007acc` | `#f0f7ff` |
| **Success** | `#1e8449` | `#eafaf1` |

### Documentation

Documentation (Markdown files, GitHub) should reference these colors in code blocks and tables as shown throughout this specification.

---

## CSS Variables Reference

For easy implementation, use CSS custom properties:

```css
:root {
    /* Background */
    --bg-primary: #1e1e1e;
    --bg-secondary: #252526;
    --bg-tertiary: #2d2d2d;
    --bg-surface: #3c3c3c;

    /* Text */
    --text-primary: #d4d4d4;
    --text-secondary: #858585;
    --text-title: #ffffff;
    --text-header: #007acc;
    --text-value: #4ec9b0;

    /* Accent */
    --accent-primary: #007acc;
    --accent-hover: #1a8ad4;
    --accent-focus: #3794ff;

    /* Border */
    --border-color: #555555;
    --border-radius: 4px;
    --border-radius-lg: 8px;

    /* Alert Levels */
    --level-critical: #f48771;
    --level-critical-bg: #5a1a1a;
    --level-warning: #dcdcaa;
    --level-warning-bg: #4d4d1a;
    --level-notice: #4ec9b0;
    --level-notice-bg: #1a4d4d;
    --level-info: #9cdcfe;
    --level-info-bg: #1a3d4d;
    --level-debug: #6a9955;
    --level-debug-bg: #1a3d1a;

    /* Status */
    --status-success: #0e8a16;
    --status-error: #d73a49;

    /* Links */
    --link-color: #3794ff;
    --link-visited: #9e6fff;

    /* Spacing */
    --spacing-xs: 4px;
    --spacing-sm: 8px;
    --spacing-md: 16px;
    --spacing-lg: 24px;
    --spacing-xl: 32px;

    /* Transitions */
    --transition-fast: 0.15s ease;
    --transition-normal: 0.2s ease;
}
```

---

## Quick Reference Card

### Core Colors (Copy-Paste Ready)

```
Background Primary:   #1e1e1e
Background Secondary: #252526
Surface/Controls:     #3c3c3c
Border:               #555555

Accent Primary:       #007acc
Accent Focus:         #3794ff

Text Primary:         #d4d4d4
Text Secondary:       #858585
Text Title:           #ffffff

Critical:             #f48771 / bg: #5a1a1a
Warning:              #dcdcaa / bg: #4d4d1a
Notice:               #4ec9b0 / bg: #1a4d4d
Info:                 #9cdcfe / bg: #1a3d4d
Debug:                #6a9955 / bg: #1a3d1a

Success:              #0e8a16
Error:                #d73a49
```

### Logo Sizes Quick Reference

```
Favicon:     16x16, 32x32, 48x48 (+ .ico)
Apple:       180x180
Android:     192x192, 512x512
Web Header:  200x60
Email:       300x90
Square:      120x120
Social OG:   1200x630
```

### Alternative Logo Palette (Copy-Paste Ready)

```
Background:           #0B1020 (Deep Navy)
Background Alt:       #111827 (Slate)

Alert Accent:         #F97316 (Amber)
Alert Accent Dark:    #EA580C (Dark Amber)

Analytics Accent:     #22D3EE (Cyan)
Analytics Alt:        #06B6D4 (Teal)

Neutral Light:        #E5E7EB (Silver)
Neutral Mid:          #9CA3AF (Gray)
```

---

## Changelog

### 1.0.1 (2025-12-01)
- Added alternative logo palette for special projects
- Typography recommendations for logo wordmark
- File naming conventions for alternative assets

### 1.0.0 (2025-12-01)
- Initial theme specification
- Dark mode color system defined
- Alert level colors standardized
- Logo resolution requirements documented
- Email template color adjustments specified
