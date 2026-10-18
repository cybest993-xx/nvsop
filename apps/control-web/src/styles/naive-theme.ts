import type { GlobalThemeOverrides } from 'naive-ui'

/**
 * Naive UI's theme, expressed in this product's design tokens.
 *
 * The component library derives its hover, pressed and subtle shades from the colours it is given,
 * so it needs concrete colours here rather than the CSS variables `tokens.css` defines — passing
 * `var(--color-primary)` would leave it nothing to compute from, and unit tests run with CSS
 * processing off, so reading the computed value at runtime would come back empty.
 *
 * That makes this file and `tokens.css` a pair: a change to a brand colour belongs in both. The
 * alternative — generating one from the other — buys a build step for four colours.
 *
 * Only the base colours, the text and line colours, and the shape live here. A rule that reaches
 * into a component's internal class names is not a theme override; it belongs in a shared
 * component.
 */
export const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#1c84c6',
    primaryColorHover: '#3a9ad9',
    primaryColorPressed: '#17699f',
    primaryColorSuppl: '#3a9ad9',

    successColor: '#1ab394',
    successColorHover: '#3ec2a7',
    successColorPressed: '#148f76',
    successColorSuppl: '#3ec2a7',

    warningColor: '#f8ac59',
    warningColorHover: '#fabc78',
    warningColorPressed: '#e29443',
    warningColorSuppl: '#fabc78',

    errorColor: '#ed5565',
    errorColorHover: '#f07280',
    errorColorPressed: '#c9424f',
    errorColorSuppl: '#f07280',

    textColorBase: '#676a6c',
    textColor1: '#2f4050',
    textColor2: '#676a6c',
    textColor3: '#a7b1c2',
    textColorDisabled: '#c2c9d1',

    borderColor: '#e7eaec',
    dividerColor: '#e7eaec',

    bodyColor: '#f3f3f4',
    cardColor: '#ffffff',
    modalColor: '#ffffff',
    popoverColor: '#ffffff',
    tableColor: '#ffffff',
    inputColor: '#ffffff',

    tableHeaderColor: '#a0d4c6',
    hoverColor: '#f6f7f8',

    borderRadius: '4px',
    borderRadiusSmall: '2px',

    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Arial, sans-serif",
  },
}
