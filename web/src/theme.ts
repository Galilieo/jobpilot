// 应用的极简雾蓝主题（07 第 2、3 节与静态原型）：所有色彩统一在这里配置，页面只读 token 或 CSS 变量。
import type { ThemeConfig } from 'antd'

export const jobpilotTheme: ThemeConfig = {
  token: {
    // 雾蓝主色与浅底（与原型 #50687F / #E4EAF0 / #3B5268 对齐）
    colorPrimary: '#50687F',
    colorPrimaryHover: '#3F566C',
    colorPrimaryActive: '#33475B',
    colorPrimaryBg: '#E4EAF0',
    colorPrimaryBgHover: '#D7E0E9',
    colorPrimaryBorder: '#C9D4DF',
    colorPrimaryBorderHover: '#9FB1C3',
    colorPrimaryText: '#3B5268',
    colorPrimaryTextHover: '#2E4357',
    colorLink: '#50687F',
    colorLinkHover: '#3F566C',

    // 信息色与主色统一，供「你填写 / 你补充的 / 最新 / 表达缺口」等雾蓝标签复用
    colorInfo: '#50687F',
    colorInfoBg: '#E4EAF0',
    colorInfoBorder: '#C9D4DF',
    colorInfoText: '#3B5268',

    // 柔和低饱和状态色（鼠尾草绿 / 暖琥珀 / 陶土红），避免刺眼默认高亮色
    colorSuccess: '#5B8A4A',
    colorSuccessBg: '#E6EEDF',
    colorSuccessBorder: '#D3E2C9',
    colorSuccessText: '#3F6130',

    colorWarning: '#B9842F',
    colorWarningBg: '#F5ECDA',
    colorWarningBorder: '#EBDBBA',
    colorWarningText: '#7A5618',

    colorError: '#B5534A',
    colorErrorBg: '#F5E3E0',
    colorErrorBorder: '#ECCDC8',
    colorErrorText: '#8A3A33',

    // 中性骨架（纯白主画布、暖纸灰面板与柔和边框）
    colorBgLayout: '#FFFFFF',
    colorBgContainer: '#FFFFFF',
    colorBgElevated: '#FFFFFF',
    colorFillQuaternary: '#FAFAF8',
    colorFillTertiary: '#F2F2F0',
    colorFillSecondary: '#EBEBE7',
    colorText: '#1F1F1F',
    colorTextSecondary: '#5F5F5B',
    colorTextTertiary: '#8F8F8A',
    colorBorder: '#E6E6E2',
    colorBorderSecondary: '#EFEFEC',

    // 字体与固定档位字号（规则 8：页面标题 22、区块 16、卡片 14、正文 14、次要 13）
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
    fontSize: 14,
    fontSizeSM: 13,
    fontSizeHeading1: 22,
    fontSizeHeading2: 18,
    fontSizeHeading3: 16,
    fontSizeHeading4: 14,
    fontSizeHeading5: 14,
    lineHeight: 1.65,
    borderRadius: 10,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    boxShadowSecondary: '0 6px 20px rgba(31, 31, 31, 0.08)',
  },
  components: {
    Typography: {
      titleMarginTop: 0,
      titleMarginBottom: 0,
    },
    Layout: {
      bodyBg: '#FFFFFF',
      siderBg: '#F6F6F4',
      headerBg: '#F6F6F4',
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#E4EAF0',
      itemSelectedColor: '#3B5268',
      itemHoverBg: 'rgba(80, 104, 127, 0.07)',
      itemColor: '#5F5F5B',
      itemBorderRadius: 10,
      itemHeight: 38,
      itemMarginInline: 0,
      activeBarBorderWidth: 0,
    },
    Card: {
      borderRadiusLG: 12,
      paddingLG: 16,
      headerFontSize: 16,
      headerHeight: 48,
    },
    Tag: {
      borderRadiusSM: 6,
      defaultBg: '#F7F7F5',
      defaultColor: '#5F5F5B',
    },
    Button: {
      primaryShadow: 'none',
      defaultShadow: 'none',
      dangerShadow: 'none',
    },
    Tabs: {
      inkBarColor: '#50687F',
      itemActiveColor: '#3B5268',
      itemSelectedColor: '#3B5268',
      itemHoverColor: '#3B5268',
      itemColor: '#5F5F5B',
      titleFontSize: 14,
    },
    Table: {
      headerBg: '#FAFAF8',
      headerColor: '#5F5F5B',
      rowHoverBg: '#FAFAF8',
    },
    Segmented: {
      trackBg: '#F2F2F0',
      itemSelectedBg: '#FFFFFF',
      itemColor: '#5F5F5B',
      itemSelectedColor: '#1F1F1F',
    },
    Alert: {
      borderRadiusLG: 10,
    },
  },
}
