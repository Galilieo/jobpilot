// 应用的「雾蓝 + 纸面」（方向 D）主题（07 第 0、2、3 节与原型 v2）：所有色彩与圆角档位统一在这里配置，页面只读 token 或 CSS 变量。
import type { ThemeConfig } from 'antd'

export const jobpilotTheme: ThemeConfig = {
  token: {
    // 雾蓝主色与浅底（方向 D）
    colorPrimary: '#46607A',
    colorPrimaryHover: '#3B5268',
    colorPrimaryActive: '#2F4357',
    colorPrimaryBg: '#EEF2F6',
    colorPrimaryBgHover: '#E2E9F0',
    colorPrimaryBorder: '#CDD6DF',
    colorPrimaryBorderHover: '#9FB1C3',
    colorPrimaryText: '#3B5268',
    colorPrimaryTextHover: '#2E4357',
    colorLink: '#46607A',
    colorLinkHover: '#3B5268',

    // 信息色与主色统一，供「你填写 / 你补充的 / 最新 / 表达缺口」等雾蓝标签复用
    colorInfo: '#46607A',
    colorInfoBg: '#EEF2F6',
    colorInfoBorder: '#CDD6DF',
    colorInfoText: '#3B5268',

    // 核对绿 / 暖琥珀 / 陶土红（方向 D）
    colorSuccess: '#2E6B58',
    colorSuccessBg: '#EAF3EE',
    colorSuccessBgHover: '#D5E9DE',
    colorSuccessBorder: '#C7DFD3',
    colorSuccessText: '#245747',

    colorWarning: '#9A6418',
    colorWarningHover: '#D9A04A',
    colorWarningBg: '#FBF2E2',
    colorWarningBorder: '#ECD9B4',
    colorWarningText: '#7D5112',

    colorError: '#B5534A',
    colorErrorBg: '#F6E8E8',
    colorErrorBorder: '#E9C8C8',
    colorErrorText: '#9B3C3C',

    // 中性骨架（纯白画布、纸面灰面板、用户气泡与柔和边框）
    colorBgLayout: '#FFFFFF',
    colorBgContainer: '#FFFFFF',
    colorBgElevated: '#FFFFFF',
    colorFillQuaternary: '#F2F4F5',
    colorFillTertiary: '#F3F5F7',
    colorFillSecondary: '#E8EDF2',
    colorText: '#1E2832',
    colorTextSecondary: '#66717D',
    colorTextTertiary: '#9DA6B0',
    colorBorder: '#E7EAED',
    colorBorderSecondary: '#E6E9EC',

    // 字体与圆角三级（小控件 5 / 卡片 8 / 输入框 14）
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
    fontSize: 14,
    fontSizeSM: 13,
    fontSizeHeading1: 22,
    fontSizeHeading2: 18,
    fontSizeHeading3: 16,
    fontSizeHeading4: 14,
    fontSizeHeading5: 14,
    lineHeight: 1.6,
    borderRadius: 8,
    borderRadiusLG: 8,
    borderRadiusSM: 5,
    boxShadowSecondary: '0 10px 28px rgba(20, 30, 40, 0.12)',
  },
  components: {
    Typography: {
      titleMarginTop: 0,
      titleMarginBottom: 0,
    },
    Layout: {
      bodyBg: '#FFFFFF',
      siderBg: '#F5F6F7',
      headerBg: '#F5F6F7',
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#E8EDF2',
      itemSelectedColor: '#26313C',
      itemHoverBg: '#E8EDF2',
      itemColor: '#26313C',
      itemBorderRadius: 5,
      itemHeight: 36,
      itemMarginInline: 0,
      activeBarBorderWidth: 0,
    },
    Card: {
      borderRadiusLG: 8,
      paddingLG: 16,
      headerFontSize: 16,
      headerHeight: 48,
    },
    Tag: {
      borderRadiusSM: 4,
      defaultBg: '#F3F5F7',
      defaultColor: '#66717D',
    },
    Button: {
      primaryShadow: 'none',
      defaultShadow: 'none',
      dangerShadow: 'none',
    },
    Tabs: {
      inkBarColor: '#46607A',
      itemActiveColor: '#1E2832',
      itemSelectedColor: '#1E2832',
      itemHoverColor: '#1E2832',
      itemColor: '#66717D',
      titleFontSize: 14,
    },
    Table: {
      headerBg: '#F2F4F5',
      headerColor: '#66717D',
      rowHoverBg: '#F2F4F5',
    },
    Segmented: {
      trackBg: '#F3F5F7',
      itemSelectedBg: '#FFFFFF',
      itemColor: '#66717D',
      itemSelectedColor: '#1E2832',
    },
    Alert: {
      borderRadiusLG: 8,
    },
  },
}
