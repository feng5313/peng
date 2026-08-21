// ==================== xterm 主题配置 ====================

// 🔧 Task#1028: 全面覆盖emoji宽度范围，避免Firefox渲染宽度与xterm cell分配不匹配
const EXTENDED_WIDE_RANGES = [
  [253, 254],          // 组合下划线、长音标记
  [10004, 10004],     // ✔ Check Mark
  [10060, 10062],     // ❌ ❎ ✅
  // Dingbats (U+2700-U+27BF): ✏✅❌✨✂ etc
  [9984, 10175],
  // Misc Symbols & Pictographs full range (U+1F300-U+1F5FF): 🔧📤📥📝🔐🔄🌀etc
  [127744, 128511],
  // Transport & Map Symbols full range (U+1F680-U+1F6FF): 🚀🛠📡etc
  [128640, 128767],
  // Supplemental Symbols & Pictographs (U+1F900-U+1F9FF)
  [129280, 129535],
  [129536, 129647],
  [129648, 129651],
  [129652, 129659],
  [129744, 129750],
  [129752, 129790],
  [129791, 129794],
];

// 预构建全局 Set，避免每次创建终端都重新构建
const EXTENDED_WIDE_CHAR_SET = new Set();
for (const [start, end] of EXTENDED_WIDE_RANGES) {
  for (let cp = start; cp <= end; cp++) {
    EXTENDED_WIDE_CHAR_SET.add(cp);
  }
}

/**
 * 创建扩展的 Unicode11 provider，修复 emoji 宽度问题
 * @param {Object} baseProvider - 基础 Unicode11 provider
 * @returns {Object} 扩展的 provider
 */
function createExtendedUnicodeV11(baseProvider) {
  return {
    version: '11-extended',
    baseProvider: baseProvider,
    extraWideCharSet: EXTENDED_WIDE_CHAR_SET,
    wcwidth(codePoint) {
      let width = this.baseProvider.wcwidth(codePoint);
      if (width === 1) {
        if (this.extraWideCharSet.has(codePoint)) {
          return 2;
        }
      }
      return width;
    }
  };
}

/**
 * 🔧 Task#1028: 在xterm终端上注册扩展Unicode11 provider
 * 兼容不同xterm版本：_activeProvider路径因版本而异
 * @param {Terminal} xterm - xterm.js Terminal实例
 */
function registerExtendedUnicodeV11(xterm) {
  try {
    // 兼容多版本路径：先尝试公共API，再尝试内部_core路径
    var provider = null;
    var uni = xterm.unicode;
    if (!uni) return;

    // 路径1: 旧版addon直接暴露 _activeProvider
    if (uni._activeProvider) {
      provider = uni._activeProvider;
    }
    // 路径2: 新版addon通过 _core.unicodeService 访问
    else if (uni._core && uni._core.unicodeService) {
      var svc = uni._core.unicodeService;
      provider = svc._activeProvider || (svc._providers && svc._providers['11']);
    }

    if (provider) {
      var extended = createExtendedUnicodeV11(provider);
      uni.register(extended);
      uni.activeVersion = '11-extended';
    }
  } catch (e) {
    console.warn('Unicode11扩展provider加载失败:', e.message);
  }
}

const XTERM_THEMES = {
  dark: {
    background: '#0d1117',
    foreground: '#e6edf3',
    cursor: '#e6edf3',
    cursorAccent: '#0d1117',
    selectionBackground: 'rgba(255, 255, 255, 0.3)',
    selectionForeground: '#e6edf3',
    black: '#2e3436',
    red: '#cc0000',
    green: '#4e9a06',
    yellow: '#c4a000',
    blue: '#3465a4',
    magenta: '#75507b',
    cyan: '#06989a',
    white: '#d3d7cf',
    brightBlack: '#555753',
    brightRed: '#ef2929',
    brightGreen: '#8ae234',
    brightYellow: '#fce94f',
    brightBlue: '#729fcf',
    brightMagenta: '#ad7fa8',
    brightCyan: '#34e2e2',
    brightWhite: '#eeeeec',
    grey: '#6e7681',          // 🔧 Task#840：行号灰色
    darkGrey: '#484f58',
  },
  light: {
    background: '#ffffff',
    foreground: '#1f2328',
    cursor: '#1f2328',
    cursorAccent: '#ffffff',
    selectionBackground: 'rgba(0, 0, 0, 0.3)',
    selectionForeground: '#1f2328',
    black: '#24292f',
    red: '#cf222e',
    green: '#1a7f37',
    yellow: '#b08800',
    blue: '#0550ae',
    magenta: '#8250df',
    cyan: '#0550ae',
    white: '#57606a',
    brightBlack: '#8c959f',      // 思考内容 - 浅灰（更淡）
    brightRed: '#a40e26',
    brightGreen: '#116329',
    brightYellow: '#6e6a00',
    brightBlue: '#0969da',
    brightMagenta: '#8250df',
    brightCyan: '#0a3069',
    brightWhite: '#24292f',     // 正式内容 - 深色（更突出）
    grey: '#8b949e',            // 🔧 Task#840：行号灰色
    darkGrey: '#6e7681',
  }
};

