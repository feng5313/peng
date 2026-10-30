// ==================== xterm终端初始化 ====================

/**
 * 清理 Windows 路径：去除尾部非路径字符（如 "、,、\n 等），并将双反斜杠替换为单反斜杠
 * 工具调用参数中的路径可能包含 JSON 转义（\\）和尾部垃圾字符（如 D:\\path\\file.txt",\n）
 * @param {string} path - 原始匹配到的路径字符串
 * @returns {string} 清理后的有效路径
 */
function cleanWindowsPath(path) {
  // 1. 剥离 null 字节及其后的 ANSI 占位符内容（如 \x00ANSI_SGR_1\x00）
  let cleaned = path.replace(/\x00.*/g, '');
  // 2. 剥离已知占位符名称（防止占位符泄露到路径中）
  cleaned = cleaned.replace(/ANSI_SGR_\d+.*$/g, '');
  cleaned = cleaned.replace(/ANSI_OSC8_\d+.*$/g, '');
  cleaned = cleaned.replace(/(?:WIN|UNIX|URL|PARAM)_MATCH_\d+.*$/g, '');
  // 3. 剥离反引号及其后内容（markdown代码围栏等）
  cleaned = cleaned.replace(/`[^`]*$/g, '');
  cleaned = cleaned.replace(/`$/g, '');
  // 4. 剥离尾部非路径字符：从引号 " 开始的尾部内容（如 "、",\n、"\\\n 等）
  cleaned = cleaned.replace(/["'].*/, '');
  // 4.5. 剥离中文标点及其后内容（如 D:\path\file.js，作用 → D:\path\file.js）
  // 中文逗号、句号、分号、冒号、感叹号、问号、顿号等都不是合法路径字符
  cleaned = cleaned.replace(/[，。；：！？、（）《》【】「」『』〈〉].*$/g, '');
  // 5. 剥离尾部逗号、分号等常见分隔符
  cleaned = cleaned.replace(/[,;:]+$/, '');
  // 6. 将双反斜杠 \\ 替换为单反斜杠 \
  cleaned = cleaned.replace(/\\\\/g, '\\');
  // 7. 将正斜杠统一为反斜杠（Windows路径）
  cleaned = cleaned.replace(/\//g, '\\');
  return cleaned;
}

/**
 * 将文件路径转换为OSC8超链接格式
 * 格式: \x1b]8;;uri\x07text\x1b]8;;\x07
 */
function convertFilePathsToLinks(text) {
  // 🔧 修复性能：快速路径检测——如果文本中没有路径特征，直接返回原文
  // 检查是否包含路径分隔符（排除纯ANSI序列的情况）
  // 大量输出行（如纯文本、JSON数据）不包含路径，跳过复杂的正则匹配可显著减少GC压力
  if (!text || (text.indexOf('/') === -1 && text.indexOf('\\') === -1 && text.indexOf('~') === -1)) {
    // 🔧 修复 Task #87：即使快速路径返回，也需要更新跨行颜色记忆
    // 扫描文本中最后一个 SGR 序列并更新 state.currentColor
    if (text) {
      let lastSGR = null;
      const sgrRegex = /\x1b\[(?:[0-9;]*)m/g;
      let m;
      while ((m = sgrRegex.exec(text)) !== null) {
        lastSGR = m[0];
      }
      if (lastSGR !== null) {
        state.currentColor = lastSGR;
      }
    }
    return text;
  }

  // 使用ANSI转义符感知的匹配策略
  // 关键：保护ANSI转义符，只在纯文本上匹配路径，最后恢复所有转义符

  let result = text;

  // 步骤0：保护所有ANSI转义符（先保护，最后恢复）
  const ansiMatches = [];
  result = result.replace(/\x1b\[[0-9;]*m/g, (match) => {
    const placeholder = `\x00ANSI_SGR_${ansiMatches.length}\x00`;
    ansiMatches.push({ placeholder, ansiCode: match });
    return placeholder;
  });
  result = result.replace(/\x1b\]8;;[^]*?\x1b\]8;;\x07/g, (match) => {
    const placeholder = `\x00ANSI_OSC8_${ansiMatches.length}\x00`;
    ansiMatches.push({ placeholder, ansiCode: match });
    return placeholder;
  });

  // 🔧 获取当前主题的蓝色（从 XTERM_THEMES 中动态获取）
  const currentTheme = XTERM_THEMES[state.theme] || XTERM_THEMES.dark;
  const blueHex = currentTheme.blue;
  // 将十六进制颜色转换为 ANSI 真彩色代码：\x1b[38;2;R;G;Bm
  const blueColor = hexToAnsiRgb(blueHex);

  /**
   * 将十六进制颜色转换为 ANSI 真彩色代码
   * @param {string} hex - 十六进制颜色（如 #3465a4）
   * @returns {string} ANSI 真彩色代码（如 \x1b[38;2;52;101;164m）
   */
  function hexToAnsiRgb(hex) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `\x1b[38;2;${r};${g};${b}m`;
  }

  // 🔧 修复：检测"逐字符包裹ANSI"模式，但不移除占位符
  // 这种模式下，每个字符都被单独包裹：\x00ANSI_SGR_0\x00s\x00ANSI_SGR_1\x00\x00ANSI_SGR_2\x00r\x00ANSI_SGR_3\x00...
  // 检测策略：统计 SGR 占位符数量，如果数量很多（相对于文本长度），则是逐字符包裹
  const sgrCount = (result.match(/\x00ANSI_SGR_\d+\x00/g) || []).length;
  const textLength = result.replace(/\x00ANSI_SGR_\d+\x00/g, '').length;
  const hasPerCharAnsi = sgrCount > textLength * 0.5;  // 如果 SGR 占位符数量超过文本长度的50%

  // 步骤1：先保护 http/https URL
  const urlMatches = [];
  result = result.replace(/https?:\/\/[^\s<>"{}|\\^`\[\]\x00]+/g, (match) => {
    // 🔧 修复 Task #54：剥离URL末尾的标点符号（英文句号、逗号、分号等）
    const tp = match.match(/[.,;:!?)\]]+$/);
    const url = tp ? match.slice(0, -tp[0].length) : match;
    const placeholder = `\x00URL_MATCH_${urlMatches.length}\x00`;
    urlMatches.push({ placeholder, url });
    return placeholder + (tp ? tp[0] : '');
  });

  // 步骤2：处理 filePath/path= 格式的参数（最优先）
  const paramMatches = [];
  result = result.replace(/(?:filePath|path)=["']([^"']+)["']/g, (match, filePath) => {
    const placeholder = `\x00PARAM_MATCH_${paramMatches.length}\x00`;
    paramMatches.push({ placeholder, filePath });
    return placeholder;
  });

  // 步骤2.5：🔧 修复 Task #52：全平台匹配 Windows 盘符路径（如 d:/downloads/test.jpg）
  // 在所有平台上启用（不仅限于 Windows 平台），因为 Linux 上也可能引用 Windows 路径
  // 注意：必须在步骤3（unix路径）之前执行，否则 unixRegex 会截取 d:/path 中的 /path 部分
  // 🔧 修复 Task #721：(?<![a-zA-Z0-9]) 防止 drive letter 是某个单词的一部分（如 matched:\n 中的 d:）
  // {2,} 要求盘符路径分隔符后至少2个字符，防止 \n \r \t 等转义序列被误匹配为路径
  const winDriveMatches = [];
  result = result.replace(/(?<![a-zA-Z0-9])([a-zA-Z]:[/\\][^\s\x00"'`，。；：！？、（）《》【】「」『』〈〉]{2,})/g, (match) => {
    if (match.startsWith('\x00')) return match;
    const cleanedPath = cleanWindowsPath(match);
    if (!cleanedPath || cleanedPath.length < 3) return match;
    if (!isValidProjectPath(cleanedPath)) return match;
    const placeholder = `\x00WINDRIVE_MATCH_${winDriveMatches.length}\x00`;
    winDriveMatches.push({ placeholder, filePath: cleanedPath });
    return placeholder;
  });

  // 步骤3：处理 Unix/Linux 路径
  //    - 绝对路径：/home/user/file.txt
  //    - 相对路径：./file.txt, ../parent/file
  //    - 家目录：~/docs/file.md
  //    - 包含路径分隔符：src/app.py, lib/utils/helper.js
  // 🔧 修复 Task #239：彻底重构路径匹配策略，不再限制固定扩展名列表
  // 旧方案用固定扩展名列表（CHINESE_FILENAME_EXT），导致：
  //   1. .json 在 .js 前面时，webui.json 被截断为 webui.js
  //   2. .jsonp 等不在列表中的扩展名，被错误截断为 .json
  // 新方案：用"文件名含点号+扩展名"的通用模式，配合负向前瞻(?![a-zA-Z0-9_\-\+])确保扩展名完整
  // 合并正则: 中文/Unicode文件名路径优先（含扩展名），纯ASCII路径其次，家目录最后
  // 🔧 修复 Task #537：文件名stem字符类移除 \. (点号)，防止贪婪匹配回溯到最后一个点号
  const unixRegex = new RegExp(
    `(?:[~/]?[a-zA-Z0-9_\\-\\.\\+]+\\/)+[a-zA-Z0-9_\\-\\+\\u0080-\\uffff]+\\.[a-zA-Z0-9_\\-\\+]+(?![a-zA-Z0-9_\\-\\+])|(?:[~/]?[a-zA-Z0-9_\\-\\.\\+]+\\/)+[a-zA-Z0-9_\\-\\.\\+]+\\/?|~\\/[^\\s]+`, 'g'
  );
  const unixMatches = [];
  // 先找到所有路径匹配，然后提取真正的路径部分（不包括占位符）
  const rawUnixMatches = [];
  let unixMatch;
  while ((unixMatch = unixRegex.exec(result)) !== null) {
    const matchText = unixMatch[0];

    // 检查1：跳过占位符本身
    if (/^\x00[A-Z_]+_\d+\x00$/.test(matchText)) continue;

    // 检查2：匹配被占位符完全包围（如 \x00ANSI_SGR_0\x00src/peng/routes/api.py\x00ANSI_SGR_1\x00）
    // 这种情况下，提取中间的路径部分
    const fullPlaceholderPattern = /^\x00[A-Z_]+_\d+\x00([a-zA-Z0-9_\-\.\+\/]+)\x00[A-Z_]+_\d+\x00$/;
    const fullPlaceholderMatch = matchText.match(fullPlaceholderPattern);
    if (fullPlaceholderMatch && fullPlaceholderMatch[1]) {
      const originalPath = fullPlaceholderMatch[1];
      // 🔧 修复 Task #54：剥离路径末尾的标点符号
      const tp = originalPath.match(/[.,;:!?)\]]+$/);
      const realPath = tp ? originalPath.slice(0, -tp[0].length) : originalPath;
      // 🔧 二次过滤：检查路径是否在项目目录/文件中
      if (isValidProjectPath(realPath)) {
        const pathOffset = fullPlaceholderMatch[0].indexOf(originalPath);
        rawUnixMatches.push({
          path: realPath,
          start: unixMatch.index + pathOffset,
          end: unixMatch.index + pathOffset + realPath.length
        });
      }
      continue;
    }

    // 检查3：匹配后面跟着占位符（如 src/peng/routes/api.py\x00ANSI_SGR_1\x00）
    const placeholderIndex = matchText.search(/\x00ANSI_|\x00URL_|\x00PARAM_|\x00WINDRIVE_/);
    let realPath = placeholderIndex >= 0 ? matchText.slice(0, placeholderIndex) : matchText;

    // 🔧 修复 Task #54：剥离路径末尾的标点符号（英文句号、逗号、分号等）
    // 如 "/tmp/opt.md." → "/tmp/opt.md"，"See /tmp/file.txt," → 路径不含逗号
    const trailingPunctuation = realPath.match(/[.,;:!?)\]]+$/);
    if (trailingPunctuation) {
      realPath = realPath.slice(0, -trailingPunctuation[0].length);
    }

    if (realPath) {
      // 🔧 二次过滤：检查路径是否在项目目录/文件中
      if (isValidProjectPath(realPath)) {
        rawUnixMatches.push({
          path: realPath,
          start: unixMatch.index,
          end: unixMatch.index + realPath.length
        });
      }
    }
  }

  // 从后往前替换，避免索引变化
  for (let i = rawUnixMatches.length - 1; i >= 0; i--) {
    const { path, start, end } = rawUnixMatches[i];
    const placeholder = `\x00UNIX_MATCH_${unixMatches.length}\x00`;
    unixMatches.push({ placeholder, filePath: path });
    result = result.slice(0, start) + placeholder + result.slice(end);
  }

  // 步骤4：根据平台处理 Windows 路径（仅 Windows 平台启用）
  //    - 绝对路径：C:\path\to\file 或 C:/path/to/file
  //    - UNC 路径：\\server\share\path
  //    - 相对路径：.\folder\file 或 ..\parent\file
  const windowsMatches = [];
  if (state.platform === 'windows') {
    // 🔧 修复：排除 null字节(\x00)、引号("')、反引号(`) 避免匹配到非路径内容
    // 🔧 修复 Task #721：(?<![a-zA-Z0-9]) 防止 drive letter 是某个单词的一部分
    result = result.replace(/(?<![a-zA-Z0-9])(?:[a-zA-Z]:[\\/][^\s\x00"'`]*)|(?:\\\\[^\\]+\\[^\\]+(?:\\[^\\]+)*)|(?:\.\.[\\/][^\s\x00]*)|(?:\.[\\/][^\s\x00]*)/g, (match) => {
      if (match.startsWith('\x00')) return match;
      // 🔧 修复：清理 Windows 路径中的双反斜杠和尾部垃圾字符
      const cleanedPath = cleanWindowsPath(match);
      if (!cleanedPath || cleanedPath.length < 3) return match;  // 清理后路径太短，放弃
      // 🔧 修复：二次过滤，检查路径是否在项目中存在
      if (!isValidProjectPath(cleanedPath)) return match;
      const placeholder = `\x00WIN_MATCH_${windowsMatches.length}\x00`;
      windowsMatches.push({ placeholder, filePath: cleanedPath });
      return placeholder;
    });
  }

  // 步骤5：恢复所有ANSI转义符到原始位置（在创建OSC8之前恢复！）
  // 这确保ANSI转义符不会被包含在OSC8超链接内部
  // 使用精确匹配：替换完整的占位符单词，避免部分匹配

  // 🔧 修复：始终恢复所有ANSI转义符（包括SGR和OSC8），保留颜色
  ansiMatches.forEach(({ placeholder, ansiCode }) => {
    const regex = new RegExp(placeholder.replace(/([.*+?^=!:${}()|[\]\/\\])/g, '\\$1'), 'g');
    result = result.replace(regex, ansiCode);
  });

  // 🔧 修复 Task #87：超链接后恢复原先颜色而非重置颜色
  // 查找指定位置之前的最后一个 SGR 颜色序列（不含超链接自身插入的蓝色）
  // 返回 null 表示当前文本中没有找到 SGR
  function findLastSGR(text, beforeIndex) {
    let lastSGR = null;  // null 表示当前文本中没有 SGR
    const regex = /\x1b\[(?:[0-9;]*)m/g;
    let match;
    while ((match = regex.exec(text)) !== null) {
      if (match.index >= beforeIndex) break;
      lastSGR = match[0];
    }
    return lastSGR;
  }

  // 🔧 修复 Task #87：收集所有待替换占位符及其在原始 result 中的 prevColor
  // 必须在替换前一次性扫描，避免前一个超链接的恢复序列污染后续搜索
  // 关键：如果当前文本中没有 SGR 代码（findLastSGR 返回 null），
  // 则使用 state.currentColor（跨行记忆的全局颜色状态）来恢复颜色
  const allReplacements = [];
  function buildReplacement(placeholder, linkTarget, displayText) {
    const idx = result.indexOf(placeholder);
    const foundSGR = idx >= 0 ? findLastSGR(result, idx) : null;
    // 优先使用当前行中找到的 SGR，其次使用跨行记忆的 state.currentColor，最后用重置前景色
    const prevColor = foundSGR || state.currentColor || '\x1b[39m';
    return { placeholder, replacement: `${blueColor}\x1b]8;;${linkTarget}\x07${displayText}\x1b]8;;\x07${prevColor}` };
  }
  urlMatches.forEach(({ placeholder, url }) => {
    allReplacements.push(buildReplacement(placeholder, url, url));
  });
  paramMatches.forEach(({ placeholder, filePath }) => {
    const uri = `editor://${filePath}`;
    allReplacements.push(buildReplacement(placeholder, uri, filePath));
  });
  unixMatches.forEach(({ placeholder, filePath }) => {
    const uri = `editor://${filePath}`;
    allReplacements.push(buildReplacement(placeholder, uri, filePath));
  });
  // 🔧 修复 Task #52：Windows 盘符路径（全平台）
  winDriveMatches.forEach(({ placeholder, filePath }) => {
    const uri = `editor://${filePath}`;
    allReplacements.push(buildReplacement(placeholder, uri, filePath));
  });
  windowsMatches.forEach(({ placeholder, filePath }) => {
    const uri = `editor://${filePath}`;
    allReplacements.push(buildReplacement(placeholder, uri, filePath));
  });

  // 统一替换所有超链接占位符
  allReplacements.forEach(({ placeholder, replacement }) => {
    result = result.replace(placeholder, replacement);
  });

  // 🔧 修复 Task #87：更新跨行颜色记忆
  // 扫描最终结果中的最后一个 SGR 序列，作为下一行的"当前颜色"
  const lastSGRInResult = findLastSGR(result, result.length);
  if (lastSGRInResult !== null) {
    state.currentColor = lastSGRInResult;
  }
  // 注意：如果当前行没有 SGR 代码，state.currentColor 保持不变（继承上一行的颜色）

  return result;
}

/**
 * 将文件路径转换为HTML超链接格式(用于消息/事务树显示)
 * 🔧 修复 Task #44：先保护URL和路径（保存原始值），再对剩余文本进行HTML转义，
 *    防止 <id> 等类似HTML标签的内容被浏览器当作真正的HTML标签解析。
 * @param {string} text - 要转换的文本
 * @returns {string} 带有HTML超链接的文本
 */
function convertFilePathsToHtmlLinks(text) {
  let result = text;

  // 步骤1：先保护 http/https URL（在原始文本上匹配，保存原始URL）
  const urlMatches = [];
  // 🔧 修复 Task #44：优化URL正则，只匹配URL中的合法字符（RFC 3986），
  // 在遇到非合法字符（如中文逗号、空格、< > 等）时停止匹配
  // 使用字符类：a-zA-Z0-9 和 URL子分隔符：-._~:/?#[]@!$&'()*+,;=%
  result = result.replace(/https?:\/\/[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+/g, (match) => {
    // 🔧 修复 Task #54：剥离URL末尾的标点符号（英文句号、逗号、分号等）
    const tp = match.match(/[.,;:!?)\]]+$/);
    const url = tp ? match.slice(0, -tp[0].length) : match;
    const placeholder = `__URL_PH_${urlMatches.length}__`;
    urlMatches.push({ placeholder, originalUrl: url });  // 保存原始URL
    return placeholder + (tp ? tp[0] : '');
  });

  // 步骤2：处理 filePath/path= 格式的参数(最优先)
  const paramMatches = [];
  result = result.replace(/(?:filePath|path)=["']([^"']+)["']/g, (match, filePath) => {
    const placeholder = `__PARAM_PH_${paramMatches.length}__`;
    paramMatches.push({ placeholder, originalPath: filePath });  // 保存原始路径
    return placeholder;
  });

  // 步骤2.5：🔧 修复 Task #52：全平台匹配 Windows 盘符路径（如 d:/downloads/test.jpg）
  // 在所有平台上启用，因为 Linux 上也可能引用 Windows 路径
  // 必须在步骤3（unix路径）之前执行
  // 🔧 修复 Task #721：(?<![a-zA-Z0-9]) 防止 drive letter 是某个单词的一部分（如 matched:\n 中的 d:）
  // {2,} 要求盘符路径分隔符后至少2个字符，防止 \n \r \t 等转义序列被误匹配为路径
  const winDriveMatches = [];
  result = result.replace(/(?<![a-zA-Z0-9])([a-zA-Z]:[/\\][^\s"'`，。；：！？、（）《》【】「」『』〈〉]{2,})/g, (match) => {
    if (match.startsWith('__')) return match;
    const cleanedPath = cleanWindowsPath(match);
    if (!cleanedPath || cleanedPath.length < 3) return match;
    if (!isValidProjectPath(cleanedPath)) return match;
    const placeholder = `__WINDRIVE_PH_${winDriveMatches.length}__`;
    winDriveMatches.push({ placeholder, originalPath: cleanedPath });
    return placeholder;
  });

  // 步骤3：处理 Unix/Linux 路径
  //    - 绝对路径：/home/user/file.txt
  //    - 相对路径：./file.txt, ../parent/file
  //    - 家目录：~/docs/file.md
  //    - 包含路径分隔符：src/app.py, lib/utils/helper.js
  // 🔧 修复 Task #239：彻底重构路径匹配策略，不再限制固定扩展名列表
  // 旧方案用固定扩展名列表，导致 .json 截断 .jsonp、.js 截断 .json 等问题
  // 新方案：通用"文件名含扩展名"模式，配合负向前瞻确保扩展名完整
  // 🔧 修复 Task #537：文件名stem字符类移除 \. (点号)
  // 旧版 [a-zA-Z0-9_\-\.\+\u0080-\uffff] 含点号，贪婪匹配后回溯到最后一个点号
  // 导致 "智慧树（特征描述）.md（主文档...）" 中最后一个点号被误判为扩展名分隔符
  const unixRegex = new RegExp(
    `(?:[~/]?[a-zA-Z0-9_\\-\\.\\+]+\\/)+[a-zA-Z0-9_\\-\\+\\u0080-\\uffff]+\\.[a-zA-Z0-9_\\-\\+]+(?![a-zA-Z0-9_\\-\\+])|(?:[~/]?[a-zA-Z0-9_\\-\\.\\+]+\\/)+[a-zA-Z0-9_\\-\\.\\+]+\\/?|~\\/[^\\s]+`, 'g'
  );
  const unixMatches = [];
  // 先找到所有路径匹配,然后提取真正的路径部分(不包括占位符)
  const rawUnixMatches = [];
  let unixMatch;
  while ((unixMatch = unixRegex.exec(result)) !== null) {
    const matchText = unixMatch[0];

    // 检查1：跳过占位符本身
    if (/^__[A-Z_]+_PH_\d+__$/.test(matchText)) continue;

    // 检查2：匹配被占位符完全包围(如 __URL_PH_0__src/peng/routes/api.py__URL_PH_1__)
    // 这种情况下,提取中间的路径部分
    const fullPlaceholderPattern = /^__[A-Z_]+_PH_\d+__([a-zA-Z0-9_\-\.\+\/]+)__[A-Z_]+_PH_\d+__$/;
    const fullPlaceholderMatch = matchText.match(fullPlaceholderPattern);
    if (fullPlaceholderMatch && fullPlaceholderMatch[1]) {
      const originalPath = fullPlaceholderMatch[1];
      // 🔧 修复 Task #54：剥离路径末尾的标点符号
      const tp = originalPath.match(/[.,;:!?)\]]+$/);
      const realPath = tp ? originalPath.slice(0, -tp[0].length) : originalPath;
      // 🔧 二次过滤：检查路径是否在项目目录/文件中
      if (isValidProjectPath(realPath)) {
        const pathOffset = fullPlaceholderMatch[0].indexOf(originalPath);
        rawUnixMatches.push({
          path: realPath,
          start: unixMatch.index + pathOffset,
          end: unixMatch.index + pathOffset + realPath.length
        });
      }
      continue;
    }

    // 检查3：匹配后面跟着占位符(如 src/peng/routes/api.py__URL_PH_1__)
    const placeholderIndex = matchText.search(/__[A-Z_]+_PH_\d+__/);
    let realPath = placeholderIndex >= 0 ? matchText.slice(0, placeholderIndex) : matchText;

    // 🔧 修复 Task #54：剥离路径末尾的标点符号（英文句号、逗号、分号等）
    // 如 "/tmp/opt.md." → "/tmp/opt.md"，"See /tmp/file.txt," → 路径不含逗号
    const trailingPunctuation = realPath.match(/[.,;:!?)\]]+$/);
    if (trailingPunctuation) {
      realPath = realPath.slice(0, -trailingPunctuation[0].length);
    }

    if (realPath) {
      // 🔧 二次过滤：检查路径是否在项目目录/文件中
      if (isValidProjectPath(realPath)) {
        rawUnixMatches.push({
          path: realPath,
          start: unixMatch.index,
          end: unixMatch.index + realPath.length
        });
      }
    }
  }

  // 从后往前替换,避免索引变化
  for (let i = rawUnixMatches.length - 1; i >= 0; i--) {
    const { path, start, end } = rawUnixMatches[i];
    const placeholder = `__UNIX_PH_${unixMatches.length}__`;
    unixMatches.push({ placeholder, originalPath: path });  // 保存原始路径
    result = result.slice(0, start) + placeholder + result.slice(end);
  }

  // 步骤4：根据平台处理 Windows 路径（仅 Windows 平台启用）
  //    - 绝对路径：C:\path\to\file 或 C:/path/to/file
  //    - UNC 路径：\\server\share\path
  //    - 相对路径：.\folder\file 或 ..\parent\file
  const windowsMatches = [];
  if (state.platform === 'windows') {
    // 🔧 修复：排除引号("')、反引号(`) 避免匹配到非路径内容
    // 🔧 修复 Task #721：(?<![a-zA-Z0-9]) 防止 drive letter 是某个单词的一部分
    result = result.replace(/(?<![a-zA-Z0-9])(?:[a-zA-Z]:[\\/][^\s"'`]*)|(?:\\\\[^\\]+\\[^\\]+(?:\\[^\\]+)*)|(?:\.\.[\\/][^\s]*)|(?:\.[\\/][^\s]*)/g, (match) => {
      if (match.startsWith('__')) return match;
      // 🔧 修复：清理 Windows 路径中的双反斜杠和尾部垃圾字符
      const cleanedPath = cleanWindowsPath(match);
      if (!cleanedPath || cleanedPath.length < 3) return match;  // 清理后路径太短，放弃
      // 🔧 修复：二次过滤，检查路径是否在项目中存在
      if (!isValidProjectPath(cleanedPath)) return match;
      const placeholder = `__WIN_PH_${windowsMatches.length}__`;
      windowsMatches.push({ placeholder, originalPath: cleanedPath });  // 保存清理后的路径
      return placeholder;
    });
  }

  // 🔧 步骤5a：保护 expand-link span 不被 HTML 转义
  // truncateText 会在截断文本末尾追加 <span class="expand-link"...>...</span>
  // 这个 HTML 需要原样保留，不能被 escapeHtml 转义
  const expandLinkMatches = [];
  result = result.replace(/<span class="expand-link"[^>]*>.*?<\/span>/g, (match) => {
    const placeholder = `__EXPAND_PH_${expandLinkMatches.length}__`;
    expandLinkMatches.push({ placeholder, originalHtml: match });
    return placeholder;
  });

  // 🔧 步骤5b：对剩余文本（已替换为不会被HTML转义的占位符）进行HTML转义
  // 这样类似 <id>、<xxx> 的内容会变成 &lt;id&gt;、&lt;xxx&gt;，显示为纯文本原样
  // 占位符 __XXX_PH_N__ 不会被转义（不包含 < > & " ' 等特殊字符）
  result = escapeHtml(result);

  // 步骤6：将 URL 占位符转换为 HTML 超链接(新窗口打开)
  urlMatches.forEach(({ placeholder, originalUrl }) => {
    result = result.replace(placeholder, `<a href="${escapeHtml(originalUrl)}" target="_blank" style="color: var(--link-color); text-decoration: none;">${escapeHtml(originalUrl)}</a>`);
  });

  // 步骤7：替换路径占位符为 editor:// HTML 超链接
  paramMatches.forEach(({ placeholder, originalPath }) => {
    const uri = `editor://${originalPath}`;
    result = result.replace(placeholder, `<a href="${escapeHtml(uri)}" style="color: var(--link-color); text-decoration: none;" onclick="handleEditorLinkClick(event, '${escapeJs(originalPath)}'); return false;">${escapeHtml(originalPath)}</a>`);
  });
  unixMatches.forEach(({ placeholder, originalPath }) => {
    const uri = `editor://${originalPath}`;
    result = result.replace(placeholder, `<a href="${escapeHtml(uri)}" style="color: var(--link-color); text-decoration: none;" onclick="handleEditorLinkClick(event, '${escapeJs(originalPath)}'); return false;">${escapeHtml(originalPath)}</a>`);
  });
  // 🔧 修复 Task #52：Windows 盘符路径（全平台）
  winDriveMatches.forEach(({ placeholder, originalPath }) => {
    const uri = `editor://${originalPath}`;
    result = result.replace(placeholder, `<a href="${escapeHtml(uri)}" style="color: var(--link-color); text-decoration: none;" onclick="handleEditorLinkClick(event, '${escapeJs(originalPath)}'); return false;">${escapeHtml(originalPath)}</a>`);
  });
  windowsMatches.forEach(({ placeholder, originalPath }) => {
    const uri = `editor://${originalPath}`;
    result = result.replace(placeholder, `<a href="${escapeHtml(uri)}" style="color: var(--link-color); text-decoration: none;" onclick="handleEditorLinkClick(event, '${escapeJs(originalPath)}'); return false;">${escapeHtml(originalPath)}</a>`);
  });

  // 步骤8：恢复 expand-link span 的 HTML（不被转义）
  expandLinkMatches.forEach(({ placeholder, originalHtml }) => {
    result = result.replace(placeholder, originalHtml);
  });

  return result;
}

/**
 * 判断文件路径是否是图片文件
 * @param {string} filePath - 文件路径
 * @returns {boolean} 是否是图片文件
 */
function isImageFile(filePath) {
  const imageExtensions = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'];
  const lowerPath = filePath.toLowerCase();
  return imageExtensions.some(ext => lowerPath.endsWith(ext));
}

/**
 * 打开图片浏览器模态框
 * @param {string} filePath - 图片文件路径
 * @param {boolean} useLocal - 是否使用本地文件系统
 * @param {string|null} foundNest - 找到文件的nest名称（用于从正确nest加载图片）
 */
async function openImageViewer(filePath, useLocal = false, foundNest = null) {
  const modal = document.getElementById('image-viewer-modal');
  const windowEl = document.getElementById('image-viewer-window');
  const img = document.getElementById('image-viewer-img');
  if (!modal || !windowEl || !img) return;

  const fileName = filePath.split('/').pop().split('\\').pop();

  // 窗口状态：位置和缩放比例
  const state = {
    imgNaturalWidth: 0,
    imgNaturalHeight: 0,
    baseWidth: 0,       // 初始窗口宽度（适应屏幕的图片宽度）
    baseHeight: 0,      // 初始窗口高度
    zoomLevel: 1,       // 相对初始大小的缩放倍数
    windowX: 0,         // 窗口左上角 X
    windowY: 0,         // 窗口左上角 Y
    isDragging: false,
    dragStartX: 0,
    dragStartY: 0,
    dragStartWindowX: 0,
    dragStartWindowY: 0,
  };

  // 图片导航：同目录图片列表和当前索引
  const dirPath = filePath.replace(/\\/g, '/').replace(/\/[^/]*$/, '') || '/';
  let imageList = [];    // 同目录中的图片文件路径列表
  let currentImageIndex = -1;

  // 获取当前目录的图片文件列表
  const loadImageList = async () => {
    try {
      const nestSelect = document.getElementById('nest-select');
      const currentNest = nestSelect ? nestSelect.value : '';
      const nestParam = currentNest ? `&nest=${encodeURIComponent(currentNest)}` : '';
      const localNestParam = useLocal ? '' : (foundNest ? `&nest=${encodeURIComponent(foundNest)}` : nestParam);
      const response = await fetch(`/api/files/list?path=${encodeURIComponent(dirPath)}${localNestParam}`);
      if (!response.ok) return;
      const data = await response.json();
      if (!data.success) return;
      const files = data.files || [];
      // 筛选图片文件并排序
      imageList = files
        .filter(f => f.type !== 'directory' && isImageFile(f.name))
        .map(f => f.path)
        .sort();
      const normalizedFilePath = filePath.replace(/\\/g, '/');
      currentImageIndex = imageList.findIndex(p => p.replace(/\\/g, '/') === normalizedFilePath);
    } catch (e) {
      console.error('获取图片列表失败:', e);
    }
  };

  // 导航到指定图片（预加载，保持旧图片直到新图片加载完成）
  const navigateToImage = (imagePath) => {
    let nestParamToUse;
    if (useLocal) {
      nestParamToUse = '';
    } else if (foundNest) {
      nestParamToUse = `&nest=${encodeURIComponent(foundNest)}`;
    } else {
      nestParamToUse = getNestParam();
    }
    const imageUrl = `/api/files/image?path=${encodeURIComponent(imagePath)}${nestParamToUse}`;
    fetch(imageUrl)
      .then(response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        return response.blob();
      })
      .then(blob => {
        // 新图片加载完成，替换旧图片（不闪烁）
        if (img.src && img.src.startsWith('blob:')) {
          URL.revokeObjectURL(img.src);
        }
        img.src = URL.createObjectURL(blob);
      })
      .catch(() => {});
  };

  // 滚轮缩放窗口大小（以鼠标位置为中心）
  const onWheel = (e) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newZoom = Math.max(0.1, Math.min(10, state.zoomLevel * delta));

    const oldW = state.baseWidth * state.zoomLevel;
    const oldH = state.baseHeight * state.zoomLevel;
    const newW = state.baseWidth * newZoom;
    const newH = state.baseHeight * newZoom;

    // 鼠标相对于窗口的位置比例
    const rect = windowEl.getBoundingClientRect();
    const mx = (e.clientX - rect.left) / oldW;
    const my = (e.clientY - rect.top) / oldH;

    // 调整窗口位置使鼠标位置不变
    state.windowX += mx * (oldW - newW);
    state.windowY += my * (oldH - newH);
    state.zoomLevel = newZoom;

    applyWindowState(windowEl, state);
  };

  // 鼠标拖动窗口
  const onMouseDown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    state.isDragging = true;
    state.dragStartX = e.clientX;
    state.dragStartY = e.clientY;
    state.dragStartWindowX = state.windowX;
    state.dragStartWindowY = state.windowY;
    windowEl.classList.add('dragging');
  };

  const onMouseMove = (e) => {
    if (!state.isDragging) return;
    state.windowX = state.dragStartWindowX + (e.clientX - state.dragStartX);
    state.windowY = state.dragStartWindowY + (e.clientY - state.dragStartY);
    applyWindowState(windowEl, state);
  };

  const onMouseUp = () => {
    state.isDragging = false;
    windowEl.classList.remove('dragging');
  };

  // Esc 关闭 + 方向键映射
  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      onClose();
      return;
    }
    if (e.key === 'ArrowLeft') {
      // 查看上一张图片
      e.preventDefault();
      if (imageList.length > 1 && currentImageIndex > 0) {
        currentImageIndex--;
        navigateToImage(imageList[currentImageIndex]);
      }
      return;
    }
    if (e.key === 'ArrowRight') {
      // 查看下一张图片
      e.preventDefault();
      if (imageList.length > 1 && currentImageIndex < imageList.length - 1) {
        currentImageIndex++;
        navigateToImage(imageList[currentImageIndex]);
      }
      return;
    }
    if (e.key === 'ArrowUp') {
      // 放大10%
      e.preventDefault();
      const newZoom = Math.min(10, state.zoomLevel * 1.1);
      const oldW = state.baseWidth * state.zoomLevel;
      const oldH = state.baseHeight * state.zoomLevel;
      const newW = state.baseWidth * newZoom;
      const newH = state.baseHeight * newZoom;
      // 以窗口中心为缩放中心
      state.windowX += (oldW - newW) / 2;
      state.windowY += (oldH - newH) / 2;
      state.zoomLevel = newZoom;
      applyWindowState(windowEl, state);
      return;
    }
    if (e.key === 'ArrowDown') {
      // 缩小10%
      e.preventDefault();
      const newZoom = Math.max(0.1, state.zoomLevel * 0.9);
      const oldW = state.baseWidth * state.zoomLevel;
      const oldH = state.baseHeight * state.zoomLevel;
      const newW = state.baseWidth * newZoom;
      const newH = state.baseHeight * newZoom;
      // 以窗口中心为缩放中心
      state.windowX += (oldW - newW) / 2;
      state.windowY += (oldH - newH) / 2;
      state.zoomLevel = newZoom;
      applyWindowState(windowEl, state);
      return;
    }
  };

  // 关闭函数
  const onClose = () => {
    modal.style.display = 'none';
    modal.classList.remove('show');
    if (img.src && img.src.startsWith('blob:')) {
      URL.revokeObjectURL(img.src);
    }
    img.src = '';
    img.onload = null;
    img.onerror = null;
    windowEl.style.display = 'none';
    windowEl.removeEventListener('wheel', onWheel);
    windowEl.removeEventListener('mousedown', onMouseDown);
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', onMouseUp);
    document.removeEventListener('keydown', onKeyDown);
    modal.removeEventListener('mousedown', onModalBgClick);
  };

  // 点击遮罩层（窗口外部）关闭
  const onModalBgClick = (e) => {
    if (e.target === modal) onClose();
  };

  // 绑定事件
  windowEl.addEventListener('wheel', onWheel, { passive: false });
  windowEl.addEventListener('mousedown', onMouseDown);
  document.addEventListener('mousemove', onMouseMove);
  document.addEventListener('mouseup', onMouseUp);
  document.addEventListener('keydown', onKeyDown);
  modal.addEventListener('mousedown', onModalBgClick);

  // 显示模态框
  modal.style.display = 'block';
  modal.classList.add('show');

  // 显示模态框（但窗口先隐藏，图片加载完成后再显示）
  modal.style.display = 'block';
  modal.classList.add('show');
  windowEl.style.display = 'none';

  // 图片加载完成后：按图片比例调整窗口大小，居中显示
  img.onload = () => {
    state.imgNaturalWidth = img.naturalWidth;
    state.imgNaturalHeight = img.naturalHeight;

    // 计算初始窗口大小：不超过 85% 屏幕，保持图片比例
    const maxW = window.innerWidth * 0.85;
    const maxH = window.innerHeight * 0.85;
    const fitScale = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight, 1);
    state.baseWidth = img.naturalWidth * fitScale;
    state.baseHeight = img.naturalHeight * fitScale;
    state.zoomLevel = 1;

    // 居中
    state.windowX = (window.innerWidth - state.baseWidth) / 2;
    state.windowY = (window.innerHeight - state.baseHeight) / 2;

    applyWindowState(windowEl, state);
    // 图片就绪后才显示窗口
    windowEl.style.display = '';
  };

  img.onerror = () => {
    // 加载失败，静默关闭
    onClose();
  };

  // 加载图片（useLocal时不带nest参数；foundNest时使用找到文件的nest；否则用当前选中nest）
  let nestParamToUse;
  if (useLocal) {
    nestParamToUse = '';
  } else if (foundNest) {
    nestParamToUse = `&nest=${encodeURIComponent(foundNest)}`;
  } else {
    nestParamToUse = getNestParam();
  }
  const imageUrl = `/api/files/image?path=${encodeURIComponent(filePath)}${nestParamToUse}`;
  fetch(imageUrl)
    .then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      return response.blob();
    })
    .then(blob => { img.src = URL.createObjectURL(blob); })
    .catch(() => {
      // 加载失败，静默关闭
      onClose();
    });

  // 异步加载同目录图片列表，用于左右键导航
  loadImageList();
}

/**
 * 应用图片查看器窗口状态（位置和大小）
 */
function applyWindowState(windowEl, state) {
  const w = state.baseWidth * state.zoomLevel;
  const h = state.baseHeight * state.zoomLevel;
  windowEl.style.left = `${state.windowX}px`;
  windowEl.style.top = `${state.windowY}px`;
  windowEl.style.width = `${w}px`;
  windowEl.style.height = `${h}px`;
}

/**
 * 判断路径是否为绝对路径（支持 Unix 和 Windows 格式）
 * @param {string} path - 文件路径
 * @returns {boolean} 是否为绝对路径
 */
function _isAbsolutePath(path) {
  // Unix 绝对路径（/开头）或家目录（~/开头）
  if (path.startsWith('/') || path.startsWith('~/')) return true;
  // Windows 绝对路径（如 C:\ 或 C:/）
  if (/^[a-zA-Z]:[\\/]/.test(path)) return true;
  return false;
}

/**
 * 处理编辑器链接点击（图片文件打开图片浏览器，其他文件打开编辑器）
 * @param {Event} event - 点击事件
 * @param {string} filePath - 文件路径
 */
async function handleEditorLinkClick(event, filePath) {
  event.preventDefault();
  event.stopPropagation();  // 🔧 阻止事件冒泡到父元素的 onclick（如 selectMessageHead）

  // 🔧 修复：仅在 Windows 平台清理路径中的双反斜杠和尾部垃圾字符
  // cleanWindowsPath 会将 / 转为 \，在 Linux/macOS 上会破坏 Unix 路径
  let cleanedPath = state.platform === 'windows' ? cleanWindowsPath(filePath) : filePath;

  // 🔧 修复 Task #565：相对路径使用 state.workdir 解析为绝对路径
  // 如事务 content 中的 "src/tree.h" 需要基于任务的 workdir 解析
  if (!_isAbsolutePath(cleanedPath) && state.workdir) {
    cleanedPath = state.workdir.replace(/\/+$/, '') + '/' + cleanedPath;
  }

  // 🔧 图片文件支持回退到本地文件系统，非图片文件只检查nest路径
  const isImage = isImageFile(cleanedPath);
  try {
    const { exists, useLocal, nest: foundNest, is_dir } = await checkFileExistsWithLocalFallback(cleanedPath, isImage);
    if (is_dir) {
      // 📁 目录：用文件浏览器打开
      openPathInFileBrowser(cleanedPath, foundNest);
      return;
    }
    if (!exists) return;

    // 🖼️ 图片文件：打开图片浏览器
    if (isImage) {
      openImageViewer(cleanedPath, useLocal, foundNest);
      return;
    }

    // 📝 文本文件：打开编辑器
    await openEditorForFile(cleanedPath, useLocal, foundNest);
  } catch (error) {
    console.warn('[EditorLink] 检查文件存在性失败:', error);
  }
}

/**
 * 转义JavaScript字符串(用于onclick属性)
 * @param {string} str - 要转义的字符串
 * @returns {string} 转义后的字符串
 */
function escapeJs(str) {
  return str
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r');
}

/**
 * 🔧 Task#1028/Task#1040: 修补 FitAddon，修复文字右侧溢出问题
 *
 * 问题：不同浏览器/字体下字符实际渲染宽度可能有亚像素级差异，
 * 导致 cols × cellWidth 略小于实际文本宽度，行末字符溢出。
 *
 * 方案：
 * 1. proposeDimensions 中减5列预留安全边距（≈40px），吸收亚像素累积差异
 * 2. 确保 scrollbarWidth 正确测量
 * 注意：不再用 CSS 覆盖 letter-spacing（Task#1040），因为它会破坏选中高亮对齐
 */
function patchFitAddonScrollbar(fitAddon) {
  if (!fitAddon || fitAddon._sbPatched) return fitAddon;
  fitAddon._sbPatched = true;

  var origPropose = fitAddon.proposeDimensions.bind(fitAddon);
  fitAddon.proposeDimensions = function () {
    var term = fitAddon._terminal;
    if (term && term._core) {
      var core = term._core;
      // 确保 scrollbarWidth 正确测量
      if (core.viewport && core.viewport.scrollBarWidth < 8 && term.options.scrollback > 0) {
        var el = document.createElement('div');
        el.style.cssText = 'position:absolute;top:-9999px;width:100px;height:100px;overflow:scroll;border:0';
        document.body.appendChild(el);
        var measured = el.offsetWidth - el.clientWidth;
        document.body.removeChild(el);
        if (measured > 0) {
          core.viewport.scrollBarWidth = measured;
        }
      }
    }
    var result = origPropose();
    // 🔧 Task#1028: 安全边距，吸收CJK/emoji亚像素渲染差异
    if (result && result.cols > 10) {
      result.cols -= 6;
    }
    return result;
  };
  return fitAddon;
}

function initOutput() {
  const outputEl = document.getElementById('output');
  if (!outputEl) {
    console.warn('output 元素未找到');
    return;
  }
  if (typeof Terminal === 'undefined') {
    console.warn('xterm.js 未加载，跳过终端初始化');
    return;
  }

  try {
    // 计算终端行数（基于容器高度）
    const containerHeight = outputEl.clientHeight;
    const lineHeight = 18;  // 行高（像素）
    const rows = Math.max(10, Math.floor(containerHeight / lineHeight));
    // 创建xterm实例（不设置 cols，让 fit addon 自动计算）
    state.output = new Terminal({
      theme: XTERM_THEMES[state.theme] || XTERM_THEMES.dark,
      fontFamily: FONT_SETTINGS.fontFamily,
      fontSize: FONT_SETTINGS.fontSizeXterm,
      lineHeight: 1.0,
      letterSpacing: 0,
      rows: rows,
      // 🔧 Task#1725：scrollback 提升到页大小10倍 —— 翻页整页渲染后，长行/宽字符 wrap
      // 可达3倍以上物理行占用，5000物理行保证整页内容完整保留在 buffer 内不丢失
      scrollback: state.webuiConfig.xterm.scrollback * 10,
      allowTransparency: false,  // 禁用透明，让主题背景色生效
      allowProposedApi: true,  // 启用实验性 API，支持更准确的 unicode/emoji 宽度计算
      cursorBlink: true,
      cursorStyle: 'block',
      wrapAround: true,  // 启用自动换行
      convertEol: true,  // 将 LF 转换为 CR+LF，确保换行正确
      linkHandler: {
        allowNonHttpProtocols: true,  // 允许非http/https协议的超链接（如editor://）
        hover: function(event, uri, range) {
          if (uri.startsWith('rollback:')) {
            // 尝试解析链接携带的详细提示（meta.hint，含回滚ID），失败则显示通用提示
            var hintText = '恢复到指定快照，覆盖当前文件内容。同期非本事务的变更将丢失。';
            var parsed = parseRollbackUriMeta(uri);
            if (parsed && parsed.hint) {
              hintText = parsed.hint;
            }
            showRollbackTooltip(event, hintText);
            // 持久化ID格式（rollback:id:<task>-<seq>）：本地无法解析，
            // 异步从服务端获取 meta 显示 hint（含回滚ID+回滚信息），带本地缓存
            if (uri.startsWith('rollback:id:')) {
              fetchRollbackHintAndUpdateTooltip(uri);
            }
          } else if (uri.startsWith('taskresume:')) {
            showRollbackTooltip(event, '点击恢复该事务继续运行');
          }
        },
        leave: function(event, uri) {
          if (uri.startsWith('rollback:') || uri.startsWith('taskresume:')) {
            hideRollbackTooltip();
          }
        },
        activate: async (event, uri, range) => {
          if (uri.startsWith('editor://')) {
            // 🔧 修复：解码 URI 中的路径，Windows 平台清理双反斜杠和尾部垃圾字符
            let filePath = decodeURIComponent(uri.replace('editor://', ''));
            if (state.platform === 'windows') {
              filePath = cleanWindowsPath(filePath);
            }

            // 🔧 修复 Task #565：相对路径使用 state.workdir 解析为绝对路径
            if (!_isAbsolutePath(filePath) && state.workdir) {
              filePath = state.workdir.replace(/\/+$/, '') + '/' + filePath;
            }

            // 🔧 图片文件支持回退到本地文件系统，非图片文件只检查nest路径
            const isImage = isImageFile(filePath);
            try {
              const { exists, useLocal, nest: foundNest, is_dir } = await checkFileExistsWithLocalFallback(filePath, isImage);
              if (is_dir) {
                // 📁 目录：用文件浏览器打开
                openPathInFileBrowser(filePath, foundNest);
              } else if (exists) {
                // 🖼️ 图片文件：打开图片浏览器
                if (isImage) {
                  openImageViewer(filePath, useLocal, foundNest);
                } else {
                  await openEditorForFile(filePath, useLocal, foundNest);
                }
              }
            } catch (error) {
              console.warn('[LinkHandler] 检查文件存在性失败:', error);
            }
          } else if (uri.startsWith('http://') || uri.startsWith('https://')) {
            window.open(uri, '_blank');
          } else if (uri.startsWith('rollback:')) {
            // 工具调用回滚链接：弹出自定义确认框
            handleRollbackLink(uri);
          } else if (uri.startsWith('taskresume:')) {
            // "继续处理"链接：恢复被回滚/撤销操作暂停的事务
            handleTaskResumeLink(uri);
          }
        }
      }
    });

    // 挂载到DOM
    state.output.open(outputEl);

    // 🔧 Task#1092：设置 xterm helper textarea 的 tabindex=-1，防止它在 tab 序列中
    // 抢占消息输入框的焦点（xterm 内部某些操作可能会 focus 这个 textarea）
    const helperTextarea = outputEl.querySelector('.xterm-helper-textarea');
    if (helperTextarea) {
      helperTextarea.setAttribute('tabindex', '-1');
    }

    // 🔧 Task#427：拦截alternate buffer切换的ANSI escape序列
    // output终端是只读的，不应进入alternate screen模式（会导致滚动条和鼠标滚动丢失）
    // 拦截 CSI ? Ps h (DECSET) 和 CSI ? Ps l (DECRST) 中的 47/1047/1049 参数
    try {
      // registerCsiHandler 的 finalHandler 参数：返回true表示已处理，不再传递给默认handler
      state.output.parser.registerCsiHandler({ final: 'h', prefix: '?' }, (params) => {
        if (params.includes(47) || params.includes(1047) || params.includes(1049)) {
          return true;  // 拦截alternate buffer切换
        }
        return false;  // 不拦截其他DECSET序列
      });
      state.output.parser.registerCsiHandler({ final: 'l', prefix: '?' }, (params) => {
        if (params.includes(47) || params.includes(1047) || params.includes(1049)) {
          return true;  // 拦截alternate buffer恢复
        }
        return false;
      });
    } catch (e) {
      console.warn('[initOutput] 注册alternate buffer拦截器失败:', e.message);
    }

    // 延迟初始化 fit addon（在 open() 之后）
    setTimeout(() => {
      if (!state.output) return;

      // xterm-addon-fit 的打包方式：window.FitAddon 是一个对象，真正的类是 window.FitAddon.FitAddon
      const FitAddonClass = (window.FitAddon && window.FitAddon.FitAddon) ||
        (self.FitAddon && self.FitAddon.FitAddon) ||
        (typeof globalThis !== 'undefined' && globalThis.FitAddon && globalThis.FitAddon.FitAddon) ||
        null;

      if (FitAddonClass) {
        try {
          state.outputFit = new FitAddonClass();
          state.output.loadAddon(state.outputFit);
          patchFitAddonScrollbar(state.outputFit);  // 🔧 Task#1028: 确保scrollbar宽度正确
        } catch (e) {
          console.warn('FitAddon 初始化失败，使用固定尺寸:', e.message);
          state.outputFit = null;
        }
      }

      // 初始化 Unicode11 addon（修复emoji和中文字符宽度问题）
      // UMD打包方式：window.Unicode11Addon 是一个对象，真正的类是 window.Unicode11Addon.Unicode11Addon
      const Unicode11AddonClass = (window.Unicode11Addon && window.Unicode11Addon.Unicode11Addon) ||
        (self.Unicode11Addon && self.Unicode11Addon.Unicode11Addon) ||
        (typeof globalThis !== 'undefined' && globalThis.Unicode11Addon && globalThis.Unicode11Addon.Unicode11Addon) ||
        null;

      if (Unicode11AddonClass) {
        try {
          const unicode11Addon = new Unicode11AddonClass();
          state.output.loadAddon(unicode11Addon);
          // 激活 Unicode 11 版本，使用更准确的字符宽度计算
          state.output.unicode.activeVersion = '11';
          // 🔧 扩展Unicode11 provider，修复emoji宽度问题
          registerExtendedUnicodeV11(state.output);
        } catch (e) {
          console.warn('Unicode11Addon 初始化失败:', e.message);
        }
      } else {
        console.warn('Unicode11Addon 未找到，emoji和中文字符可能显示不正确');
      }

      // fit()触发首次渲染
      if (state.outputFit) {
        try { state.outputFit.fit(); } catch(e) {}
      }
    }, 100);

    // 🔧 Task #92：监听 xterm scroll 事件
    state.output.onScroll(() => {
      if (!state.output) return;
      const buffer = state.output.buffer.active;
      const currentBufferLength = buffer.length;
      // 🔧 Fix #1064: 用 buffer长度追踪区分"程序写入引起的scroll"和"用户滚动引起的scroll"
      if (currentBufferLength !== state._lastBufferLength) {
        state._lastBufferLength = currentBufferLength;
        return;  // 程序写入引起的 scroll，跳过
      }
      if (state._pagingRecoveryUntil && Date.now() < state._pagingRecoveryUntil) return;
      // 🔧 Fix #1428: 用户刚触发滚轮上滚时，并发 write() 的 auto-scroll 会把 viewport 拉回底部，
      // 触发 onScroll → isExactlyAtBottom()==true → 误重置 userScrolledUp=false。
      // 用 _userScrollIntentUntil 时间窗口保护：用户滚动意图期间，不因 onScroll 重置 userScrolledUp。
      if (state._userScrollIntentUntil && Date.now() < state._userScrollIntentUntil) {
        return;  // 用户滚动意图时间窗口内，不修改 userScrolledUp
      }
      // 🔧 Fix Task#1108: buffer 到 scrollback 上限时 buffer.length 不变，
      // 但 write() 的 auto-scroll 仍会触发 onScroll。
      // 如果当前在底部且 userScrolledUp 已为 false，说明是 write 引起的 auto-scroll，跳过。
      // 这防止了 buffer 满时 write-induced onScroll 误设 userScrolledUp=true 的竞态。
      if (!state.userScrolledUp && isExactlyAtBottom()) {
        return;
      }
      const wasScrolledUp = state.userScrolledUp;
      // 🔧 Fix Task#1096：使用精确底部检测
      state.userScrolledUp = !isExactlyAtBottom();
      // 🔧 Task#1725：翻页模式下用户手动滚回底部 → 自动退出翻页恢复实时。
      // 此前 _browsingHistory 只有点"⏬回底"按钮才清除，用户滚轮上翻进入翻页模式后
      // 再手动滚回底部会永远卡在翻页模式 → 实时输出不再写入 → "不显示新输出"。
      if (state._browsingHistory && !state._loadingPage && isExactlyAtBottom()) {
        state._browsingHistory = false;
        state._pagingStartLine = -1;
        state._pagingEndLine = -1;
        scrollToNewest();
      }
      // 🔧 Task#816：翻页浏览历史时不触发自动回到底部
      if (wasScrolledUp && !state.userScrolledUp && !state._browsingHistory) {
        const currentHistory = getOutputHistory(state.currentTaskId);
        if (currentHistory.length > 0) {
          const latestTimestamp = currentHistory[currentHistory.length - 1].timestamp;
          if (state.currentNewestTimestamp !== null && state.currentNewestTimestamp < latestTimestamp) {
            scrollToNewest();
          }
        }
      }
    });

    // 🔧 Task#816：滚轮自动翻页
    // 用 stopPropagation 阻止事件传播到 xterm，避免冲突
    let _wheelPagingLock = false;
    outputEl.addEventListener('wheel', (e) => {
      if (!state.output) return;
      if (_wheelPagingLock) return;  // 锁定期间不处理，让 xterm 正常滚动

      const scrollingUp = e.deltaY < 0;
      const scrollingDown = e.deltaY > 0;
      const atTop = isAtTop();
      const atBottom = isAtBottom();

      // 🔧 Fix Task#1096：在 wheel 事件中直接捕获用户滚动意图
      // 核心问题：当 buffer 不满一页时（bufLen <= rows），没有滚动区，
      // scrollLines() 不改变 ydisp → onScroll 不触发 → userScrolledUp 永远为 false
      // → appendOutput/output_batch 的 shouldSkipWrite=false → 写入+scrollToBottom 拉回底部
      // 解决方案：在 wheel 事件中直接设置 userScrolledUp，不依赖 onScroll
      //
      // 🔧 Fix Task#1108：仅在确实有可滚动内容（viewport上方有内容 或 有更旧的历史页）时
      // 才设置 userScrolledUp=true。避免在内容不足一页时，微小上滚误触发 userScrolledUp
      // 导致后续输出被跳过、形成死锁。
      if (scrollingUp && !state._browsingHistory) {
        const buf = state.output.buffer.active;
        const hasScrollableAbove = buf.viewportY > 0 || buf.length > state.output.rows;
        if (hasScrollableAbove || hasOlderOutput()) {
          state.userScrolledUp = true;
          // 🔧 Fix #1428: 设置用户滚动意图时间窗口（500ms），
          // 防止并发 write() 的 auto-scroll 触发 onScroll 误重置 userScrolledUp
          state._userScrollIntentUntil = Date.now() + 500;
        }
      }

      // 非边界处的正常滚动不干涉（让 xterm 内部滚动处理）
      if (!((scrollingUp && atTop) || (scrollingDown && atBottom))) return;

      // 在边界处真正翻页时：阻止事件传播到 xterm，避免冲突
      e.stopPropagation();

      if (scrollingUp && atTop) {
        // 已在顶部，继续向上滚 → 加载上一页
        if (!hasOlderOutput()) {
          showSimpleToast('⏹️ 没有更旧的输出了', 'info');
          _wheelPagingLock = true;
          setTimeout(function() { _wheelPagingLock = false; }, 600);
          return;
        }
        _wheelPagingLock = true;
        // 🔧 Task#1725：缓存命中渲染<5ms，锁300ms足够防连击；
        // 后端请求路径由 _loadingPage 防重入，不依赖本锁时长
        loadPrevPage().finally(function() {
          setTimeout(function() { _wheelPagingLock = false; }, 300);
        });
      } else if (scrollingDown && atBottom) {
        if (hasNewerOutput()) {
          // 还有更新的输出页 → 加载下一页
          _wheelPagingLock = true;
          loadNextPage().finally(function() {
            setTimeout(function() { _wheelPagingLock = false; }, 300);
          });
        } else {
          // 已是最后一页 → 执行"回到底部"（scrollToNewest 内部统一处理状态重置）
          showSimpleToast('⏹️ 已回到底部', 'info');
          state._userScrollIntentUntil = 0;  // 🔧 Fix #1428：清除滚动意图标记
          scrollToNewest();
          _wheelPagingLock = true;
          setTimeout(function() { _wheelPagingLock = false; }, 300);
        }
      }
    }, { capture: true, passive: true });

    // 监听鼠标松开事件，实现选中后自动复制
    const outputElForListener = document.getElementById('output');
    if (outputElForListener) {
      outputElForListener.addEventListener('mouseup', () => {
        // 延迟一点时间，确保 selection 已更新
        setTimeout(() => {
          const selectedText = state.output.getSelection();
          if (selectedText) {
            // 尝试使用 Clipboard API
            if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
              navigator.clipboard.writeText(selectedText).then(() => {
                showSimpleToast('📝  已复制选中文本到剪贴板', 'success');
              }).catch(err => {
                console.warn('Clipboard API 复制失败，使用备用方法:', err);
                fallbackCopy(selectedText);
              });
            } else {
              fallbackCopy(selectedText);
            }
          } else {
            // 🔧 Task#1402：xterm 区域点击但无文本选中时，把焦点还给消息输入框
            // mouseup 在 click 之前触发，此时恢复焦点，
            // 后续的 click 事件中 focus.js 也会再次确认焦点状态
            const mainInput = document.getElementById('message-input');
            if (mainInput && document.activeElement !== mainInput) {
              mainInput.focus();
            }
          }
        }, 50);
      });
    }

    function fallbackCopy(text) {
      // 备用复制方法：使用 textarea 和 execCommand
      const textArea = document.createElement('textarea');
      textArea.value = text;
      textArea.style.position = 'fixed';
      textArea.style.top = '0';
      textArea.style.left = '0';
      textArea.style.width = '2em';
      textArea.style.height = '2em';
      textArea.style.padding = '0';
      textArea.style.border = 'none';
      textArea.style.outline = 'none';
      textArea.style.boxShadow = 'none';
      textArea.style.background = 'transparent';
      textArea.style.opacity = '0';
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      try {
        document.execCommand('copy');
        showSimpleToast('📝  已复制选中文本到剪贴板', 'success');
      } catch (err) {
        console.warn('execCommand 复制失败:', err);
        showSimpleToast('❌  复制失败: ' + err.message, 'error');
      }
      document.body.removeChild(textArea);
    }

    // 使用 ResizeObserver 监听容器大小变化（推荐方式）
    // 🔧 Task#291修复：使用**双 requestAnimationFrame** 延迟 fit 调用
    // 当容器从 display:none 变为可见时，ResizeObserver 会触发，但此时容器的实际尺寸
    // 可能还未计算完成（特别是在嵌套 flex 布局中）。
    // 单次 rAF 只能保证进入渲染管线，但 flex 布局的尺寸计算可能需要多帧才能完成。
    // 使用双 rAF 确保：第一帧完成重排(reflow)，第二帧拿到正确的容器尺寸。
    const resizeObserver = new ResizeObserver(() => {
      if (state.outputFit) {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            try {
              if (state.outputFit) {
                state.outputFit.fit();
              }
            } catch (e) {
              console.warn('fit 失败:', e);
            }
          });
        });
      }
    });
    resizeObserver.observe(outputEl);

  } catch (error) {
    console.error('终端初始化失败:', error);
    state.output = null;
  }
}

// ==================== 工具调用回滚 ====================

/**
 * 解析回滚链接URI中的rollback_meta（供hover提示等轻量场景使用）
 * rollback:BASE64_JSON 格式直接解码；rollback:id:<hex> 格式无法本地解码，返回null
 * @param {string} uri - rollback:BASE64_JSON 或 rollback:id:<hex>
 * @returns {object|null} rollback_meta 对象，解析失败返回null
 */
function parseRollbackUriMeta(uri) {
  try {
    if (uri.startsWith('rollback:id:')) {
      // 短ID格式：meta存于服务端内存池，本地无法解析（点击时会通过API获取）
      return null;
    }
    var b64 = uri.replace('rollback:', '');
    // URL-safe base64 → standard base64
    b64 = b64.replace(/-/g, '+').replace(/_/g, '/');
    // 补齐padding
    while (b64.length % 4) b64 += '=';
    var metaJson = atob(b64);
    // 处理UTF-8编码
    var metaJsonStr = decodeURIComponent(escape(metaJson));
    return JSON.parse(metaJsonStr);
  } catch (e) {
    return null;
  }
}

/**
 * 回滚超链接 hover tooltip
 */
var _rollbackTooltipEl = null;

function showRollbackTooltip(event, text) {
  hideRollbackTooltip();
  _rollbackTooltipEl = document.createElement('div');
  _rollbackTooltipEl.style.cssText = [
    'position:fixed', 'z-index:99998',
    'background:rgba(13,17,23,0.95)', 'color:#e6edf3',
    'border:1px solid #30363d', 'border-radius:6px',
    'padding:6px 10px', 'font-size:12px', 'font-family:Consolas,monospace',
    'max-width:360px', 'line-height:1.5',
    'box-shadow:0 4px 12px rgba(0,0,0,0.4)',
    'pointer-events:none', 'white-space:pre-line',
    'word-break:break-all'
  ].join(';');
  _rollbackTooltipEl.textContent = text;
  document.body.appendChild(_rollbackTooltipEl);

  // 定位到鼠标附近
  var x = event.clientX + 12;
  var y = event.clientY - 32;
  // 边界检查
  var tw = _rollbackTooltipEl.offsetWidth;
  var th = _rollbackTooltipEl.offsetHeight;
  if (x + tw > window.innerWidth) x = window.innerWidth - tw - 8;
  if (y < 8) y = event.clientY + 16;
  _rollbackTooltipEl.style.left = x + 'px';
  _rollbackTooltipEl.style.top = y + 'px';
}

function hideRollbackTooltip() {
  if (_rollbackTooltipEl) {
    _rollbackTooltipEl.remove();
    _rollbackTooltipEl = null;
  }
}

/**
 * 持久化回滚ID格式（rollback:id:<task>-<seq>）的 hover hint 异步加载
 * 从服务端 /api/rollback-meta/<id> 获取 meta，显示 hint（回滚信息+回滚ID）。
 * 带本地缓存（Map），避免反复 hover 同一链接时请求风暴；
 * 结果到达时若 tooltip 仍显示该URI则更新内容，否则丢弃。
 */
var _rollbackHintCache = new Map();  // {uri: hint|null}
var _rollbackHintPending = new Set();  // 去重进行中的请求

function fetchRollbackHintAndUpdateTooltip(uri) {
  // 命中缓存：直接更新
  if (_rollbackHintCache.has(uri)) {
    var cached = _rollbackHintCache.get(uri);
    if (cached && _rollbackTooltipEl) {
      _rollbackTooltipEl.textContent = cached;
    }
    return;
  }
  if (_rollbackHintPending.has(uri)) return;
  _rollbackHintPending.add(uri);

  var metaId = uri.replace('rollback:id:', '');
  fetch('/api/rollback-meta/' + encodeURIComponent(metaId))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _rollbackHintPending.delete(uri);
      var hint = null;
      if (data && data.success && data.rollback_meta && data.rollback_meta.hint) {
        hint = data.rollback_meta.hint;
      } else if (data && data.success && data.rollback_meta) {
        // meta存在但无hint：显示回滚ID基础信息
        var m = data.rollback_meta;
        var rid = m.rollback_id || metaId;
        hint = '回滚ID: R' + rid +
               '\n恢复到指定快照，覆盖当前文件内容。同期非本事务的变更将丢失。';
      }
      _rollbackHintCache.set(uri, hint);
      // 结果到达时tooltip仍打开才更新（鼠标可能已离开）
      if (hint && _rollbackTooltipEl) {
        _rollbackTooltipEl.textContent = hint;
      }
    })
    .catch(function() {
      _rollbackHintPending.delete(uri);
      _rollbackHintCache.set(uri, null);  // 失败也缓存，避免重试风暴
    });
}

/**
 * 处理回滚链接点击：弹出自定义确认框，确认后调用API执行回滚
 * @param {string} uri - rollback:BASE64_JSON 格式的URI
 */
function handleRollbackLink(uri) {
  // 检查是否是新格式 rollback:id:<hex>（服务端存储池引用）
  if (uri.startsWith('rollback:id:')) {
    var metaId = uri.replace('rollback:id:', '');
    handleRollbackLinkById(metaId);
    return;
  }

  try {
    // 解码base64获取rollback_meta
    // 后端使用 urlsafe_b64encode（-_替换+/），需要还原为标准base64
    var b64 = uri.replace('rollback:', '');
    // URL-safe base64 → standard base64
    b64 = b64.replace(/-/g, '+').replace(/_/g, '/');
    // 补齐padding
    while (b64.length % 4) b64 += '=';
    var metaJson = atob(b64);
    // 处理UTF-8编码
    var metaJsonStr = decodeURIComponent(escape(metaJson));
    var rollbackMeta = JSON.parse(metaJsonStr);

    var toolName = rollbackMeta.tool_name || 'unknown';
    var isUndo = toolName.startsWith('撤销:');
    var timestamp = rollbackMeta.timestamp || '';
    var taskId = rollbackMeta.task_id || '';
    var rollbackCmd = rollbackMeta.rollback_cmd || '';
    var backupType = rollbackMeta.type || '';
    var changedFiles = rollbackMeta.changed_files || [];
    var addedFiles = rollbackMeta.added_files || [];

    // 弹出自定义确认框（禁止使用系统confirm）
    // 🔧 options：四维度勾选状态（files/external/checklist/context，正向语义）
    showRollbackConfirmDialog(toolName, timestamp, taskId, rollbackCmd, backupType, changedFiles, addedFiles, isUndo, function(options) {
      // 用户确认后调用API
      executeRollback(rollbackMeta, options);
    }, rollbackMeta);
  } catch (e) {
    console.error('[Rollback] 解析回滚链接失败:', e);
    showSimpleToast('❌ 回滚链接解析失败: ' + e.message, 'error');
  }
}

/**
 * 处理新格式回滚链接（rollback:id:<hex>）
 * 先从服务端获取完整 rollback_meta，再弹确认框
 * @param {string} metaId - 服务端存储池中的 meta ID
 */
async function handleRollbackLinkById(metaId) {
  try {
    showSimpleToast('⏳ 正在获取回滚信息...', 'info');
    var response = await fetch('/api/rollback-meta/' + encodeURIComponent(metaId));
    if (!response.ok) {
      showSimpleToast('❌ 获取回滚信息失败: HTTP ' + response.status, 'error');
      return;
    }
    var data = await response.json();
    if (!data.success || !data.rollback_meta) {
      showSimpleToast('❌ 回滚信息不存在或已过期', 'error');
      return;
    }

    var rollbackMeta = data.rollback_meta;
    var toolName = rollbackMeta.tool_name || 'unknown';
    var isUndo = toolName.startsWith('撤销:');
    var timestamp = rollbackMeta.timestamp || '';
    var taskId = rollbackMeta.task_id || '';
    var rollbackCmd = rollbackMeta.rollback_cmd || '';
    var backupType = rollbackMeta.type || '';
    var changedFiles = rollbackMeta.changed_files || [];
    var addedFiles = rollbackMeta.added_files || [];

    // 🔧 options：四维度勾选状态（files/external/checklist/context，正向语义）
    showRollbackConfirmDialog(toolName, timestamp, taskId, rollbackCmd, backupType, changedFiles, addedFiles, isUndo, function(options) {
      executeRollback(rollbackMeta, options);
    }, rollbackMeta);
  } catch (e) {
    console.error('[Rollback] 获取回滚信息失败:', e);
    showSimpleToast('❌ 获取回滚信息失败: ' + e.message, 'error');
  }
}

/**
 * 显示回滚确认对话框（自定义JS弹窗，禁止使用系统confirm）
 * @param {string} toolName - 工具名称
 * @param {string} timestamp - 备份时间
 * @param {string} rollbackCmd - 回滚命令行
 * @param {string} backupType - 备份类型（git/non_git）
 * @param {array} changedFiles - 变更文件列表
 * @param {function} onConfirm - 用户确认后的回调
 */
function showRollbackConfirmDialog(toolName, timestamp, taskId, rollbackCmd, backupType, changedFiles, addedFiles, isUndo, onConfirm, rollbackMeta) {
  // 移除已有的对话框（防止重复）
  var existing = document.getElementById('rollback-confirm-dialog');
  if (existing) existing.remove();

  // 时间戳去掉ISO格式的T分隔符
  timestamp = timestamp ? timestamp.replace('T', ' ') : timestamp;

  var typeIcon = '↩️';  // 与回滚超链接图标一致
  var actionLabel = isUndo ? '撤销回滚' : '回滚';
  var actionColor = isUndo ? '#3fb950' : '#d29922';  // 撤销用绿色，回滚用橙色

  // 创建遮罩层
  var overlay = document.createElement('div');
  overlay.id = 'rollback-confirm-dialog';
  overlay.style.cssText = [
    'position:fixed', 'top:0', 'left:0', 'width:100%', 'height:100%',
    'background:rgba(0,0,0,0.6)', 'z-index:99999',
    'display:flex', 'align-items:center', 'justify-content:center',
    'font-family:Consolas,monospace'
  ].join(';');

  // 创建对话框
  var dialog = document.createElement('div');
  var theme = (typeof XTERM_THEMES !== 'undefined') ? (XTERM_THEMES[state.theme] || XTERM_THEMES.dark) : null;
  var bg = theme ? (theme.background || '#0d1117') : '#0d1117';
  var border = theme ? (theme.brightBlack || '#30363d') : '#30363d';
  var text = theme ? (theme.foreground || '#e6edf3') : '#e6edf3';
  var accent = '#d29922';
  var danger = '#f85149';
  var success = '#3fb950';

  // 宽度=xterm宽度×1.1（窗口resize自适应：vw基准+xterm实际宽度动态取小值）
  var rollbackDialogWidth = function() {
    var xt = document.getElementById('output');
    var xtW = xt ? xt.clientWidth : 0;
    var w = xtW ? xtW * 1.1 : Math.round(window.innerWidth * 0.92);
    // 不超过视口宽度（留8px边距），最小420px
    return Math.max(420, Math.min(w, window.innerWidth - 8)) + 'px';
  };
  dialog.style.cssText = [
    'background:' + bg, 'border:1px solid ' + border, 'border-radius:8px',
    'padding:20px 24px', 'width:' + rollbackDialogWidth(), 'max-width:none',
    'max-height:82vh', 'display:flex', 'flex-direction:column',
    'box-shadow:0 8px 32px rgba(0,0,0,0.5)', 'color:' + text,
    'font-family:var(--font-mono, Consolas, monospace)'  // 遵循config字体设置（含CJK fallback）
  ].join(';');

  // 窗口resize时自适应宽度（对话框存在期间）
  var resizeHandler = function() {
    if (!document.body.contains(dialog)) {
      window.removeEventListener('resize', resizeHandler);
      return;
    }
    dialog.style.width = rollbackDialogWidth();
  };
  window.addEventListener('resize', resizeHandler);

  var headerEl = document.createElement('div');

  // 🔧 回滚内容勾选框（事务#2476）：四维度——工作目录文件/外部文件/
  // checklist/上下文，默认全选；快照不含的维度disabled置灰。
  // 各维度可用性由 rollback_meta 字段动态判断（与后端 rollback 实际
  // 恢复对象一一对应）：
  // - 工作目录文件：type=git/non_git（workdir 内文件快照存在）
  // - 外部文件：external_files 非空（工具快照/undo）或 external_head_commit（迭代基线）
  // - checklist/上下文：history_commit 存在（二者同源于 history 备份 commit）
  var scopeAvail = {
    files: backupType === 'git' || backupType === 'non_git',
    external: !!((rollbackMeta && rollbackMeta.external_files && rollbackMeta.external_files.length)
      || (rollbackMeta && rollbackMeta.external_head_commit)),
    checklist: !!(rollbackMeta && rollbackMeta.history_commit),
    context: !!(rollbackMeta && rollbackMeta.history_commit)
  };
  // 撤销快照可能不带 history（备份失败时），context/checklist 照常按字段判断
  var scopeLabels = { files: '工作目录文件', external: '外部文件', checklist: 'checklist', context: '上下文' };

  // 🔧 文件级勾选列表（事务#2513）：静态清单（changed_files含added ∪
  // external_files）+ 预览动态清单 merge（迭代快照等无静态清单——文件列表
  // 由 /api/tool-rollback-preview 动态检测的 changed_files/external_changed_
  // files + external_manifest 提供，见 loadRollbackDiffPreview 的 merge）。
  // rollbackTags: {path: 'added'|'ext'}——[新增/删除]/[外部]标记
  var rollbackExtFiles = (rollbackMeta && rollbackMeta.external_files) || [];
  var rollbackTags = {};
  (addedFiles || []).forEach(function(f) { rollbackTags[f] = 'added'; });
  var rollbackFileList = (changedFiles || []).slice();
  rollbackExtFiles.forEach(function(f) {
    if (rollbackFileList.indexOf(f) < 0) {
      rollbackFileList.push(f);
      if (!rollbackTags[f]) rollbackTags[f] = 'ext';
    }
  });

  // 文件勾选行 HTML（rollbackFileList[i] + 标记）——初始渲染与动态重建共用
  var fileCheckRowsHtml = function() {
    return rollbackFileList.map(function(f) {
      var tagKind = rollbackTags[f] || '';
      var tag = tagKind === 'added'
        ? (isUndo ? ' <span style="color:#3fb950">[新增]</span>' : ' <span style="color:#f85149">[删除]</span>')
        : (tagKind === 'ext' ? ' <span style="color:#39c5cf">[外部]</span>' : '');
      var fAttr = String(f).replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      var fText = String(f).replace(/</g, '&lt;').replace(/>/g, '&gt;');
      return '<label style="display:flex;align-items:flex-start;gap:6px;cursor:pointer;font-size:var(--font-size-ui, 12px);color:' + text + ';padding:1px 0;word-break:break-all">'
        + '<input type="checkbox" class="rollback-file-check" data-file="' + fAttr + '" checked style="accent-color:' + accent + ';cursor:pointer;flex-shrink:0;width:13px;height:13px;margin-top:2px">'
        + '<span>' + fText + tag + '</span>'
        + '</label>';
    }).join('');
  };

  headerEl.innerHTML = ''
    + '<div style="font-size:16px;font-weight:bold;margin-bottom:12px;color:' + accent + '">'
    +   typeIcon + ' 确认' + actionLabel + '操作'
    + '</div>'
    + '<div style="font-size:var(--font-size-ui, 13px);line-height:1.6;margin-bottom:16px">'
    +   '<div style="margin-bottom:8px">'
    +     '<span style="color:#8b949e">快照来源：</span>'
    +     '<span style="color:' + accent + '">事务#' + taskId + ' @ ' + timestamp + '</span>'
    +   '</div>'
    +   (rollbackCmd
      ?   '<div style="margin-bottom:8px">'
      +     '<span style="color:#8b949e">命令：</span>'
      +     '<code style="background:rgba(255,255,255,0.08);padding:2px 6px;border-radius:3px;font-size:var(--font-size-ui, 12px);word-break:break-all">'
      +       rollbackCmd.replace(/</g, '&lt;').replace(/>/g, '&gt;')
      +     '</code>'
      +   '</div>'
      : '')
    // 🔧 文件区块始终渲染（空清单隐藏）——迭代快照初始无静态清单，
    // 预览动态清单到达后填充显示（mergeRollbackFiles → renderFileCheckList）
    +   '<div id="rollback-file-section" style="margin-bottom:8px' + (rollbackFileList.length ? '' : ';display:none') + '">'
    +     '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
    +       '<span style="color:#8b949e">' + (isUndo ? '恢复文件' : '影响文件') + '（<span id="rollback-file-count" style="color:#39c5cf">' + rollbackFileList.length + '/' + rollbackFileList.length + '</span>）：</span>'
    +       '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:var(--font-size-ui, 12px);color:' + text + '">'
    +         '<input type="checkbox" id="rollback-file-all" checked style="accent-color:' + accent + ';cursor:pointer;flex-shrink:0;width:13px;height:13px">'
    +         '<span>全选</span>'
    +       '</label>'
    +     '</div>'
    +     '<div id="rollback-file-list" style="margin-top:4px;max-height:160px;overflow-y:auto;background:rgba(255,255,255,0.04);border-radius:4px;padding:6px 8px">'
    +       fileCheckRowsHtml()
    +     '</div>'
    +   '</div>'
    + '</div>';

  // 🔧 动态重建文件勾选列表（预览清单到达/变化时）——保留用户已勾选状态
  // （按 data-file 路径匹配；新文件默认勾选），完成后刷新计数/联动
  var renderFileCheckList = function() {
    var section = document.getElementById('rollback-file-section');
    var listEl = document.getElementById('rollback-file-list');
    if (!listEl) return;
    if (!rollbackFileList.length) {
      if (section) section.style.display = 'none';
      return;
    }
    if (section) section.style.display = '';
    listEl.innerHTML = fileCheckRowsHtml();
    if (typeof updateScopeUi === 'function') updateScopeUi();
  };

  // 🔧 merge 预览动态清单（迭代快照无静态清单的核心修复）：
  // newFiles 追加进 rollbackFileList（去重），tags 补标记，重建列表
  var mergeRollbackFiles = function(newFiles, tags) {
    var changed = false;
    (newFiles || []).forEach(function(f) {
      if (rollbackFileList.indexOf(f) < 0) {
        rollbackFileList.push(f);
        changed = true;
      }
      var t = tags && tags[f];
      if (t && !rollbackTags[f]) {
        rollbackTags[f] = t;
        changed = true;
      }
    });
    if (changed) renderFileCheckList();
    return changed;
  };

  // diff预览区（异步加载回滚将导致的 checklist 与文件变更）
  // 字体遵循全局配置：var(--font-mono)含CJK fallback，var(--font-size-xterm)为终端字号
  var diffSectionEl = document.createElement('div');
  diffSectionEl.innerHTML = ''
    + '<div id="rollback-diff-title" style="font-size:var(--font-size-ui, 13px);margin-bottom:6px;color:#8b949e">📋 正在计算回滚diff...</div>'
    + '<div id="rollback-diff-container" style="flex:1;min-height:60px;max-height:38vh;overflow:auto;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:4px;padding:8px 10px;font-family:var(--font-mono, Consolas, monospace);font-size:var(--font-size-xterm, 12px);line-height:1.45;white-space:pre">'
    +   '<span data-elapsed style="color:#8b949e">⏳ 正在计算回滚diff...</span>'
    + '</div>';
  var diffContainer = diffSectionEl.querySelector('#rollback-diff-container');

  var footerEl = document.createElement('div');

  // 🔧 回滚内容勾选框（事务#2476）：四维度默认全选；快照不含的维度
  // disabled置灰并标注"快照不含"（勾选无意义）。上下文带警示括注
  // （快照后对话将清除——唯一有"丢失感"后果的维度）
  var scopeDesc = {
    files: '工作目录内文件',
    external: '工作目录外文件',
    checklist: '事务检查清单',
    context: '对话记忆（快照后对话将清除）'
  };
  var scopeOrder = ['files', 'external', 'checklist', 'context'];
  var scopeChecksHtml = scopeOrder.map(function(scope) {
    var avail = scopeAvail[scope];
    var dim = avail ? text : '#6e7681';
    var label = scopeLabels[scope];
    var desc = scopeDesc[scope];
    return '<label for="rollback-scope-' + scope + '" '
      + 'style="display:flex;align-items:center;gap:6px;cursor:' + (avail ? 'pointer' : 'not-allowed')
      + ';font-size:var(--font-size-ui, 12px);color:' + dim + ';padding:2px 0">'
      +   '<input type="checkbox" id="rollback-scope-' + scope + '" data-scope="' + scope + '" '
      +     (avail ? 'checked' : '') + (avail ? '' : ' disabled')
      +     ' style="accent-color:' + accent + ';cursor:pointer;flex-shrink:0;width:14px;height:14px">'
      +   '<span style="font-weight:bold">' + label + '</span>'
      +   '<span style="color:#8b949e">' + (avail ? desc : '快照不含') + '</span>'
      + '</label>';
  }).join('');
  footerEl.innerHTML = ''
    + '<div style="margin-top:12px;padding:8px 12px;background:rgba(210,153,34,0.08);border:1px solid rgba(210,153,34,0.25);border-radius:4px">'
    +   '<div style="font-size:var(--font-size-ui, 12px);color:' + accent + ';margin-bottom:4px;font-weight:bold">🔧 回滚内容（勾选要恢复到快照时刻的内容）：</div>'
    +   scopeChecksHtml
    +   '<div id="rollback-scope-summary" style="font-size:var(--font-size-ui, 12px);margin-top:6px;color:#39c5cf"></div>'
    + '</div>'
    + '<div style="margin-top:12px;padding:8px 12px;background:rgba(' + (isUndo ? '63,185,80' : '210,153,34') + ',0.1);border-radius:4px;font-size:var(--font-size-ui, 12px);color:' + (isUndo ? success : accent) + '">'
    +   (isUndo
      ?   '⚠️ 撤销将恢复到回滚前的状态。'
      :   '⚠️ 将恢复到事务#' + taskId + ' @ ' + timestamp + ' 的快照。同期非本事务的变更将丢失，只能从其他事务的快照中找回。回滚后可通过"撤销"超链接恢复。')
    + '</div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">'
    +   '<button id="rollback-cancel-btn" style="padding:6px 16px;border:1px solid ' + border + ';background:transparent;color:' + text + ';border-radius:4px;cursor:pointer;font-size:var(--font-size-ui, 13px)">取消</button>'
    +   '<button id="rollback-ok-btn" style="padding:6px 16px;border:none;background:' + actionColor + ';color:#fff;border-radius:4px;cursor:pointer;font-size:var(--font-size-ui, 13px);font-weight:bold">确认' + actionLabel + '</button>'
    + '</div>';

  dialog.appendChild(headerEl);
  dialog.appendChild(diffSectionEl);
  dialog.appendChild(footerEl);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  // 异步加载diff预览（失败不阻塞确认流程）
  if (rollbackMeta && diffContainer) {
    loadRollbackDiffPreview(rollbackMeta, diffContainer);
  } else if (diffContainer) {
    diffContainer.innerHTML = '<span style="color:#8b949e">（无回滚元数据，无法预览diff）</span>';
  }

  // 取消按钮
  document.getElementById('rollback-cancel-btn').addEventListener('click', function() {
    overlay.remove();
  });

  // 🔧 文件级勾选（事务#2513）：文件勾选框读取与选中文件列表——
  // 全选→null（不过滤=全量回滚，与旧行为一致）；部分勾选→勾选文件数组
  var getFileChecks = function() {
    return Array.prototype.slice.call(
      document.querySelectorAll('#rollback-file-list .rollback-file-check')
    );
  };
  var readSelectedFiles = function() {
    var checks = getFileChecks();
    if (!checks.length) return null;  // 无文件列表（history_only 等）→全量
    var checked = checks.filter(function(cb) { return cb.checked; });
    if (checked.length === checks.length) return null;  // 全选=不过滤
    return checked.map(function(cb) {
      return cb.getAttribute('data-file');
    });
  };

  // 🔧 四维度勾选状态：返回 {files, external, checklist, context}
  // （true=勾选回滚该维度；快照不含的disabled维度为false——后端本就无该内容可回滚；
  //  🔧 文件级联动：存在文件列表且全部未勾选时，文件两维度视同未勾选——
  //  无任何文件将回滚，与后端 selected_files 空数组防御一致）
  var readScopeOptions = function() {
    var opts = {};
    scopeOrder.forEach(function(scope) {
      var el = document.getElementById('rollback-scope-' + scope);
      opts[scope] = !!(el && el.checked);
    });
    var checks = getFileChecks();
    if (checks.length && !checks.some(function(cb) { return cb.checked; })) {
      opts.files = false;
      opts.external = false;
    }
    return opts;
  };

  // 勾选状态 → 汇总行 + 确认按钮可用性 + diff预览区块显隐联动
  var summaryEl = document.getElementById('rollback-scope-summary');
  var okBtn = document.getElementById('rollback-ok-btn');
  var updateScopeUi = function() {
    // 🔧 文件勾选计数 + 全选主checkbox三态
    var checks = getFileChecks();
    var checkedCount = checks.filter(function(cb) { return cb.checked; }).length;
    var hasFileList = checks.length > 0;
    var anyFileChecked = checkedCount > 0;
    var countEl = document.getElementById('rollback-file-count');
    if (countEl) countEl.textContent = checkedCount + '/' + checks.length;
    var allCb = document.getElementById('rollback-file-all');
    if (allCb) {
      allCb.checked = checkedCount === checks.length;
      allCb.indeterminate = checkedCount > 0 && checkedCount < checks.length;
    }
    // 维度有效性 = 勾选 且（无文件列表 或 有勾选文件——文件级联动）
    var effectiveScope = function(scope) {
      var el = document.getElementById('rollback-scope-' + scope);
      if (!el || !el.checked) return false;
      if (hasFileList && !anyFileChecked
          && (scope === 'files' || scope === 'external')) return false;
      return true;
    };
    var selected = scopeOrder.filter(effectiveScope);
    if (summaryEl) {
      if (selected.length) {
        summaryEl.textContent = '将回滚：' + selected.map(function(s) { return scopeLabels[s]; }).join('、');
        summaryEl.style.color = '#39c5cf';
      } else {
        summaryEl.textContent = '未选择任何回滚内容，请至少勾选一项';
        summaryEl.style.color = '#f85149';
      }
    }
    if (okBtn) {
      okBtn.disabled = selected.length === 0;
      okBtn.style.opacity = selected.length ? '1' : '0.5';
      okBtn.style.cursor = selected.length ? 'pointer' : 'not-allowed';
    }
    // diff预览区块联动：勾选的维度显示对应区块，取消勾选隐藏
    scopeOrder.forEach(function(scope) {
      var show = effectiveScope(scope);
      var blocks = diffContainer.querySelectorAll('[data-scope-block="' + scope + '"]');
      blocks.forEach(function(b) { b.style.display = show ? '' : 'none'; });
    });
    // 🔧 文件级联动：未勾选文件的 diff 块（data-diff-file）与预览
    // 列表行（data-file-row）隐藏（路径斜杠归一化匹配）
    var normPath = function(p) { return String(p).replace(/\\/g, '/'); };
    checks.forEach(function(cb) {
      var f = cb.getAttribute('data-file');
      var disp = cb.checked ? '' : 'none';
      var fn = normPath(f);
      diffContainer.querySelectorAll('[data-diff-file]').forEach(function(b) {
        if (normPath(b.getAttribute('data-diff-file')) === fn) b.style.display = disp;
      });
      diffContainer.querySelectorAll('[data-file-row]').forEach(function(row) {
        if (normPath(row.getAttribute('data-file-row')) === fn) row.style.display = disp;
      });
    });
  };
  scopeOrder.forEach(function(scope) {
    var el = document.getElementById('rollback-scope-' + scope);
    if (el) el.addEventListener('change', updateScopeUi);
  });
  // 文件勾选 change（容器事件委托）
  var fileListEl = document.getElementById('rollback-file-list');
  if (fileListEl) {
    fileListEl.addEventListener('change', function(e) {
      if (e.target && e.target.classList
          && e.target.classList.contains('rollback-file-check')) {
        updateScopeUi();
      }
    });
  }
  // 全选主checkbox：一键全选/全不选
  var allFileCb = document.getElementById('rollback-file-all');
  if (allFileCb) {
    allFileCb.addEventListener('change', function() {
      getFileChecks().forEach(function(cb) { cb.checked = allFileCb.checked; });
      updateScopeUi();
    });
  }

  // 确认按钮：读取四维度勾选状态 + 选中文件列表，以 options 对象传给回调
  okBtn.addEventListener('click', function() {
    if (okBtn.disabled) return;
    var options = readScopeOptions();
    options.selectedFiles = readSelectedFiles();  // null=全选全量
    overlay.remove();
    if (onConfirm) onConfirm(options);
  });

  // 初始化汇总行/按钮状态
  updateScopeUi();

  // 🔧 挂载联动函数供 diff 预览异步渲染完成后调用（维度区块+文件级
  // diff块/列表行统一显隐；updateScopeUi 已定义，异步访问时安全）
  diffContainer._updateRollbackUi = updateScopeUi;
  // 🔧 挂载动态清单 merge 函数（迭代快照无静态清单——预览动态检测的
  // 文件清单到达后填充勾选列表）
  diffContainer._mergeRollbackFiles = mergeRollbackFiles;

  // 点击遮罩层取消
  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) overlay.remove();
  });

  // ESC键取消
  var escHandler = function(e) {
    if (e.key === 'Escape') {
      overlay.remove();
      document.removeEventListener('keydown', escHandler);
    }
  };
  document.addEventListener('keydown', escHandler);
}

/**
 * diff文本着色渲染为HTML（回滚确认窗口公用）
 * 着色规则：- 行红色（回滚后移除）、+ 行绿色（回滚后恢复）、@@ 青色、文件头黄色
 * @param {string} diffText - 纯文本diff
 * @returns {string} HTML字符串
 */
function renderDiffHtml(diffText) {
  var RED = '#f85149', GREEN = '#3fb950', YELLOW = '#d29922',
      CYAN = '#39c5cf', DIM = '#8b949e';
  return diffText.split('\n').map(function(line) {
    var esc = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    var color;
    if (line.startsWith('@@')) color = CYAN;
    else if (line.startsWith('---') || line.startsWith('+++')) color = YELLOW;
    else if (line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file')) color = DIM;
    else if (line.startsWith('-')) color = RED;
    else if (line.startsWith('+')) color = GREEN;
    else color = DIM;
    return '<div style="color:' + color + '">' + (esc || '&nbsp;') + '</div>';
  }).join('');
}

/**
 * 🔧 按文件分块渲染 diff（事务#2513）：每个文件块包 data-diff-file 容器——
 * 确认窗口文件勾选框联动隐藏未勾选文件的 diff（着色同 renderDiffHtml）。
 * 按 "diff --git a/x b/x" 文件头行分割；非标准 git diff 格式（无文件头）
 * 整体渲染不包 data-diff-file（不参与文件级联动，显示不受影响）。
 * @param {string} diffText - 纯文本diff（git diff 格式）
 * @returns {string} HTML字符串
 */
function renderDiffByFile(diffText) {
  var blocks = [];
  var cur = null;
  diffText.split('\n').forEach(function(line) {
    if (line.indexOf('diff --git ') === 0) {
      var m = line.match(/^diff --git a\/(.+) b\/(.+)$/);
      cur = { file: (m && m.length > 2) ? m[2] : '', lines: [line] };
      blocks.push(cur);
    } else if (cur) {
      cur.lines.push(line);
    }
  });
  if (!blocks.length) {
    return renderDiffHtml(diffText);
  }
  return blocks.map(function(b) {
    return '<div data-diff-file="' + String(b.file).replace(/"/g, '&quot;') + '">'
      + renderDiffHtml(b.lines.join('\n'))
      + '</div>';
  }).join('');
}

/**
 * 🔧 解析 diff 中快照后新增的文件（事务#2513）——"new file mode" 块
 * 这些文件回滚时将被删除，勾选列表标 [删除]（撤销标 [新增]）。
 * @param {string} diffText - git diff 格式文本
 * @returns {string[]} 新增文件路径列表（b/ 侧路径）
 */
function parseDiffAddedFiles(diffText) {
  var added = [];
  if (!diffText || diffText.indexOf('diff --git ') < 0) return added;
  var chunks = diffText.split(/(?=diff --git )/);
  chunks.forEach(function(chunk) {
    if (chunk.indexOf('new file mode') < 0) return;
    var m = chunk.match(/^diff --git a\/(.+) b\/(.+)$/m);
    if (m && m.length > 2 && added.indexOf(m[2]) < 0) added.push(m[2]);
  });
  return added;
}

/**
 * 加载回滚diff预览并渲染到确认对话框
 * 调用 /api/tool-rollback-preview 获取回滚将导致的全部变更：
 * - checklist diff（快照备份了checklist时，对比当前checklist与快照checklist）
 * - 文件变更 diff（git diff）
 * 着色渲染：- 行红色（回滚后移除）、+ 行绿色（回滚后恢复）、@@ 青色、文件头黄色
 * @param {object} rollbackMeta - 回滚元数据
 * @param {HTMLElement} container - diff渲染容器
 */
async function loadRollbackDiffPreview(rollbackMeta, container, attempt) {
  attempt = attempt || 0;
  var titleEl = document.getElementById('rollback-diff-title');
  // 加载耗时计时器：大仓库/远程nest并发时diff计算可达数秒~分钟级，
  // 实时显示已耗时秒数让用户知道在计算而非卡死
  var startTs = Date.now();
  var elapsedTimer = setInterval(function() {
    if (!document.body.contains(container)) {
      clearInterval(elapsedTimer);
      return;
    }
    var ph = container.querySelector('[data-elapsed]');
    if (ph) {
      var secs = Math.round((Date.now() - startTs) / 1000);
      var txt = '⏳ 正在计算回滚diff...（已用时 ' + secs + 's，大仓库或并发构建时较慢）';
      // 🔧 事务#2513：>30s 追加繁忙指引（预览仅参考，可直接确认——
      // 确认时后端会重新检测实际变更）
      if (secs > 30) {
        txt += '\n⏳ 系统繁忙（同目录事务快照/检测可能排队中）。\n提示：diff仅供预览参考，可直接点击"确认回滚"（确认时将重新检测实际变更），无需等待。';
      }
      ph.textContent = txt;
    }
  }, 1000);
  // 🔧 事务#2513：fetch 超时上限 150s（服务端锁等待60s+计算180s兜底，
  // 防无限等待——超时显示重试入口）
  var abortCtrl = new AbortController();
  var abortTimer = setTimeout(function() { abortCtrl.abort(); }, 150000);
  try {
    var response = await fetch('/api/tool-rollback-preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rollback_meta: rollbackMeta }),
      signal: abortCtrl.signal
    });
    if (!response.ok) {
      container.innerHTML = '<span style="color:#8b949e">（diff预览不可用：HTTP ' + response.status + '）</span>';
      return;
    }
    var data = await response.json();

    // 对话框可能已被用户关闭
    if (!document.body.contains(container)) return;

    // 🔧 SWR pending（事务#2513）：服务端后台计算中——1.5s 后自动重试
    // 拉取（后台算完写入缓存，下次请求秒回；窗口打开永远 <1s 有响应）
    if (data.pending) {
      if (attempt < 40) {
        var ph = container.querySelector('[data-elapsed]');
        if (ph) {
          ph.textContent = '⏳ 正在后台计算回滚diff...（已用时 '
            + Math.round((Date.now() - startTs) / 1000)
            + 's，计算完成自动刷新；可直接点击"确认回滚"无需等待）';
        }
        setTimeout(function() {
          if (document.body.contains(container)) {
            loadRollbackDiffPreview(rollbackMeta, container, attempt + 1);
          }
        }, 1500);
      } else {
        container.innerHTML = '<span style="color:#8b949e">（diff后台计算超时，可直接确认回滚——确认时将重新检测实际变更）</span>';
      }
      return;
    }

    var YELLOW = '#d29922', DIM = '#8b949e';
    var parts = [];
    var hasChecklistDiff = false;
    var hasFileDiff = false;
    var hasExternalDiff = false;

    // 字节数人性化显示（KB/MB）
    function fmtBytes(n) {
      if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + 'MB';
      if (n >= 1024) return (n / 1024).toFixed(1) + 'KB';
      return n + 'B';
    }

    // 🔧 区块容器（事务#2476）：每个区块包 data-scope-block 容器——
    // 勾选框取消对应维度时隐藏对应区块（updateScopeUi 联动）
    function scopeBlock(scope, inner) {
      return '<div data-scope-block="' + scope + '">' + inner + '</div>';
    }

    // 1. checklist diff 区块（available=False 表示快照未备份checklist，不显示）
    var cl = data.checklist_diff;
    if (cl && cl.available && cl.changed && cl.diff) {
      hasChecklistDiff = true;
      var clInner = '<div style="color:#39c5cf;margin:2px 0 1px">☑️ checklist 变更：</div>'
        + renderDiffHtml(cl.diff);
      if (cl.truncated) {
        clInner += '<div style="color:' + YELLOW + '">⚠️ checklist diff过长已截断，还有 ' + fmtBytes(cl.truncated_bytes || 0) + ' 内容未显示</div>';
      }
      parts.push(scopeBlock('checklist', clInner));
    }

    // 2. 文件 diff 区块（success=False 时显示原因；diff为空显示message）
    // 🔧 external 类型快照：changed_files 即外部文件变更（preview 直接返回），
    // 文件diff归"外部文件"维度；其余类型归"工作目录文件"维度
    var isExternalType = rollbackMeta && rollbackMeta.type === 'external';
    var extChanged = data.external_changed_files || [];
    var fileScope = isExternalType ? 'external' : 'files';
    if (data.success) {
      if (data.diff) {
        hasFileDiff = true;
        // 🔧 按文件分块（事务#2513）：文件勾选框联动隐藏未勾选文件的diff
        var diffInner = '<div style="color:#39c5cf;margin:' + (hasChecklistDiff ? '8px' : '2px') + ' 0 1px">📄 工作目录文件变更：</div>'
          + renderDiffByFile(data.diff);
        if (data.truncated) {
          diffInner += '<div style="color:' + YELLOW + ';margin-top:4px">⚠️ diff过长已截断，还有 ' + fmtBytes(data.truncated_bytes || 0) + ' 内容未显示，完整内容请查看备份分支</div>';
        }
        parts.push(scopeBlock(fileScope, diffInner));
      } else if (!(data.changed_files && data.changed_files.length) && data.message && data.message !== 'ok') {
        // 无文件变更（diff与变更列表均空）的fallback说明
        parts.push(scopeBlock(fileScope, '<div style="color:' + DIM + '">📄 工作目录文件：' + data.message + '</div>'));
      }
    } else {
      // 文件diff不可用（history_only无文件快照、备份类型不支持、git错误等）
      if (!(data.changed_files && data.changed_files.length)) {
        parts.push(scopeBlock(fileScope, '<div style="color:' + DIM + '">📄 工作目录文件：' + (data.message || 'diff预览不可用') + '</div>'));
      }
    }

    // 3. 🔧 变更文件列表区块（无 diff 文本只有列表时渲染）——
    //    按"工作目录文件/外部文件"两维度拆分（勾选框联动）：
    //    - 外部文件段：external_changed_files（后端独立字段）；
    //      external 类型快照 changed_files 全归外部
    //    - 工作目录文件段：changed_files 排除外部文件后的剩余
    //    🔧 文件级联动（事务#2513）：每行包 data-file-row——文件勾选框
    //    联动隐藏未勾选文件行
    function renderFileList(list, scopeKey, title) {
      if (!list || !list.length) return '';
      var inner = '<div style="color:#39c5cf;margin:' + ((hasChecklistDiff || hasFileDiff) ? '8px' : '2px') + ' 0 1px">📁 ' + title + '（' + list.length + '，回滚将恢复到快照版本）：</div>'
        + list.slice(0, 50).map(function(f) {
            return '<div data-file-row="' + String(f).replace(/"/g, '&quot;') + '" style="font-size:var(--font-size-ui,12px);color:#c9d1d9;padding:1px 0 1px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + String(f).replace(/"/g, '&quot;') + '">• ' + String(f).replace(/</g, '&lt;') + '</div>';
          }).join('');
      if (list.length > 50) {
        inner += '<div style="font-size:var(--font-size-ui,12px);color:' + DIM + ';padding:1px 0 1px 12px">... 共 ' + list.length + ' 个文件</div>';
      }
      return scopeBlock(scopeKey, inner);
    }

    if (isExternalType) {
      // external 快照：changed_files 全部是外部文件
      if (data.changed_files && data.changed_files.length) {
        hasExternalDiff = true;
        parts.push(renderFileList(data.changed_files, 'external', '外部文件'));
      }
    } else {
      // 外部文件段（external_changed_files 独立字段）
      if (extChanged.length) {
        hasExternalDiff = true;
        parts.push(renderFileList(extChanged, 'external', '外部/非git管理文件'));
      }
      // 工作目录文件段（changed_files 排除外部文件；仅 diff 为空时渲染——
      // 有 diff 时变更已在上方 diff 区块展示，列表冗余）
      if (!data.diff && data.changed_files && data.changed_files.length) {
        var wdList = data.changed_files.filter(function(f) {
          return extChanged.indexOf(f) < 0;
        });
        if (wdList.length) {
          hasFileDiff = true;
          parts.push(renderFileList(wdList, 'files', '工作目录文件变更'));
        }
      }
    }

    if (parts.length) {
      container.innerHTML = parts.join('');
    } else {
      container.innerHTML = '<span style="color:' + DIM + '">✅ 无任何变更</span>';
    }

    // 更新区块标题（汇总各类变更的有无）
    if (titleEl && document.body.contains(titleEl)) {
      var titles = [];
      if (hasChecklistDiff) titles.push('checklist');
      if (hasFileDiff) titles.push('文件');
      if (hasExternalDiff) titles.push('外部文件');
      titleEl.textContent = titles.length
        ? '📋 回滚变更diff（' + titles.join(' + ') + '）：'
        : '📋 回滚变更diff：';
    }

    // 🔧 文件级勾选动态清单 merge（事务#2513 修复迭代快照无静态清单）：
    // 预览动态检测的变更文件（changed_files 排除外部项）+ 外部变更
    // （external_changed_files，标[外部]）+ 迭代 manifest（restore_all
    // 恢复对象，即使无变化也覆盖恢复）+ diff 中 new file（快照后新增→
    // 回滚删除，标[删除]）并入勾选列表——保留用户已勾选状态
    if (container._mergeRollbackFiles) {
      var mergeTags = {};
      var mergeList = [];
      (data.changed_files || []).forEach(function(f) {
        if (extChanged.indexOf(f) >= 0) return;  // 外部项统一走 extChanged
        if (isExternalType) { mergeList.push(f); mergeTags[f] = 'ext'; }
        else mergeList.push(f);
      });
      extChanged.forEach(function(f) {
        if (mergeList.indexOf(f) < 0) mergeList.push(f);
        mergeTags[f] = 'ext';
      });
      var extManifest = (rollbackMeta && rollbackMeta.external_manifest) || [];
      extManifest.forEach(function(f) {
        if (mergeList.indexOf(f) < 0) { mergeList.push(f); mergeTags[f] = 'ext'; }
      });
      if (data.diff) {
        parseDiffAddedFiles(data.diff).forEach(function(f) {
          mergeTags[f] = 'added';
          if (mergeList.indexOf(f) < 0) mergeList.push(f);
        });
      }
      container._mergeRollbackFiles(mergeList, mergeTags);
    }

    // 🔧 预览区块渲染后按当前勾选状态应用显隐（预览异步加载完成时
    // 用户可能已取消勾选某些维度/文件）——调用对话框的联动函数
    // （维度区块 + 文件级 diff 块/列表行，统一由 updateScopeUi 处理）
    if (container._updateRollbackUi) {
      container._updateRollbackUi();
    }
  } catch (e) {
    console.error('[Rollback] diff预览加载失败:', e);
    if (document.body.contains(container)) {
      // 🔧 事务#2513：150s 超时——繁忙提示+重试（可直接确认回滚）
      if (e && (e.name === 'AbortError' || /aborted/i.test(String(e.message)))) {
        container.innerHTML = ''
          + '<div style="color:#d29922">⏳ diff预览计算超时（系统繁忙：同目录事务快照/检测可能排队中）。</div>'
          + '<div style="color:#8b949e;margin:4px 0">提示：diff仅供预览参考，可直接点击"确认回滚"（确认时将重新检测实际变更），无需等待。</div>'
          + '<button id="rollback-diff-retry" style="padding:4px 14px;border:1px solid #30363d;background:transparent;color:#e6edf3;border-radius:4px;cursor:pointer;font-size:var(--font-size-ui, 12px)">重试预览</button>';
        var retryBtn = container.querySelector('#rollback-diff-retry');
        if (retryBtn) {
          retryBtn.addEventListener('click', function() {
            container.innerHTML = '<span data-elapsed style="color:#8b949e">⏳ 正在计算回滚diff...</span>';
            loadRollbackDiffPreview(rollbackMeta, container);
          });
        }
      } else {
        container.innerHTML = '<span style="color:#8b949e">（diff预览加载失败：' + e.message + '）</span>';
      }
    }
  } finally {
    clearTimeout(abortTimer);
    clearInterval(elapsedTimer);
  }
}

/**
 * 调用API执行回滚
 * @param {object} rollbackMeta - 回滚元数据
 * @param {object} [options] - 回滚内容勾选状态（事务#2476 四勾选框，正向直传）：
 *   {files, external, checklist, context}——true=回滚该维度，false=不回滚；
 *   selectedFiles（事务#2513 文件级勾选）——null=全选全量（默认，与旧行为
 *   一致），数组=只回滚选中的文件（空数组=文件全不选）
 */
async function executeRollback(rollbackMeta, options) {
  try {
    showSimpleToast('⏳ 正在执行回滚...', 'info');
    options = options || {};
    var response = await fetch('/api/tool-rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rollback_meta: rollbackMeta,
        rollback_files: !!options.files,
        rollback_external: !!options.external,
        rollback_checklist: !!options.checklist,
        rollback_context: !!options.context,
        selected_files: options.selectedFiles || null
      })
    });

    var data = await response.json();

    if (data.success) {
      // 回滚记录已由后端 OutputManager 写入 xterm，前端仅显示toast
      showSimpleToast(data.undo_display || data.message || '回滚成功', 'success');
    } else {
      showSimpleToast('❌ ' + (data.message || '回滚失败'), 'error');
    }
  } catch (e) {
    console.error('[Rollback] 回滚API调用失败:', e);
    showSimpleToast('❌ 回滚失败: ' + e.message, 'error');
  }
}

/**
 * 处理"继续处理"超链接点击（taskresume:<task_id> 格式的URI）
 * 回滚/撤销操作暂停运行中的事务后，输出中的绿色"继续处理"链接，
 * 点击后恢复（resume）该事务继续运行。
 * 复用 message-actions.js 的 resumeTask()（调用 /api/tasks/{id}/action resume）
 * @param {string} uri - taskresume:<task_id>
 */
function handleTaskResumeLink(uri) {
  var taskId = parseInt(uri.replace('taskresume:', ''), 10);
  if (!taskId || isNaN(taskId)) {
    console.error('[TaskResume] 解析继续处理链接失败:', uri);
    showSimpleToast('❌ 继续处理链接解析失败', 'error');
    return;
  }
  if (typeof resumeTask !== 'function') {
    console.error('[TaskResume] resumeTask 函数不可用');
    showSimpleToast('❌ 恢复功能不可用', 'error');
    return;
  }
  resumeTask(taskId);
}
