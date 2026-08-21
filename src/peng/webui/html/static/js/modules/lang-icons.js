/**
 * 文件类型 / 语言 统一图标映射
 *
 * 供文件浏览器、最近文件列表、编辑器头部语言图标统一使用。
 * 通过文件扩展名或语言ID获取对应的emoji图标。
 */

// 扩展名 → emoji 图标
var FILE_EXT_ICONS = {
  // Python
  'py': '🐍', 'pyi': '🐍', 'pyw': '🐍',
  // Web
  'js': '📜', 'mjs': '📜', 'cjs': '📜',
  'ts': '📘', 'tsx': '📘', 'd.ts': '📘',
  'html': '🌐', 'htm': '🌐',
  'css': '🎨', 'scss': '🎨', 'sass': '🎨', 'less': '🎨',
  'vue': '💚', 'svelte': '🔥',
  // C/C++
  'c': 'C', 'h': 'H', 'cpp': '󰙲 ', 'hpp': '󰙲 ',
  'cc': '󰙲 ', 'cxx': '󰙲 ', 'hxx': '󰙲 ', 'inl': '󰙲 ',
  'cppm': '󰙲 ', 'ixx': '󰙲 ',
  // Rust
  'rs': '🦀',
  // Go
  'go': '🐹',
  // Java
  'java': '☕', 'kt': '🟣', 'kts': '🟣',
  // C#
  'cs': '💜',
  // Ruby
  'rb': '💎', 'erb': '💎',
  // PHP
  'php': '🐘',
  // Swift
  'swift': '🐦',
  // Zig
  'zig': '⚡',
  // Lua
  'lua': '🌙',
  // Shell
  'sh': '🐚', 'bash': '🐚', 'zsh': '🐚', 'fish': '🐚',
  'bat': '🪟', 'cmd': '🪟', 'ps1': '🪟',
  // Config
  'json': '📝', 'jsonc': '📝',
  'yaml': '⚙️', 'yml': '⚙️',
  'toml': '⚙️', 'ini': '⚙️', 'cfg': '⚙️', 'conf': '⚙️',
  'xml': '📋', 'svg': '🖼️',
  'env': '🔧',
  // Doc
  'md': '📝', 'mdx': '📝', 'rst': '📝', 'txt': '📄', 'log': '📄',
  'pdf': '📕', 'doc': '📘', 'docx': '📘', 'xls': '📗', 'xlsx': '📗',
  'ppt': '📙', 'pptx': '📙',
  // Image
  'png': '🖼️', 'jpg': '🖼️', 'jpeg': '🖼️', 'gif': '🖼️',
  'webp': '🖼️', 'bmp': '🖼️', 'ico': '🖼️', 'tiff': '🖼️',
  // Archive
  'zip': '📦', 'tar': '📦', 'gz': '📦', 'bz2': '📦', 'xz': '📦',
  '7z': '📦', 'rar': '📦',
  // Data
  'sql': '🗃️', 'db': '🗃️', 'sqlite': '🗃️',
  'csv': '📊', 'tsv': '📊',
  // Build
  'makefile': '🔨', 'cmake': '🔨', 'dockerfile': '🐳',
  // Lock
  'lock': '🔒',
};

// 语言ID → emoji 图标（编辑器头部使用）
var LANG_ID_ICONS = {
  'python': '🐍',
  'javascript': '📜',
  'typescript': '📘',
  'html': '🌐',
  'css': '🎨',
  'json': '📝',
  'markdown': '📝',
  'c': 'C',
  'cpp': '󰙲 ',
  'rust': '🦀',
  'go': '🐹',
  'java': '☕',
  'ruby': '💎',
  'php': '🐘',
  'swift': '🐦',
  'zig': '⚡',
  'lua': '🌙',
  'shell': '🐚',
  'yaml': '⚙️',
  'toml': '⚙️',
  'sql': '🗃️',
  'xml': '📋',
  'vue': '💚',
  'plaintext': '📄',
};

/**
 * 通过文件名获取图标emoji
 * @param {string} filename - 文件名或路径
 * @returns {string} emoji图标
 */
function getFileIcon(filename) {
  if (!filename) return '📄';
  var name = filename.split('/').pop().split('\\').pop().toLowerCase();

  // 特殊文件名匹配
  var specialNames = {
    'makefile': '🔨', 'gnumakefile': '🔨',
    'cmakelists.txt': '🔨', 'cmakecache.txt': '🔨',
    'dockerfile': '🐳', '.dockerignore': '🐳',
    '.gitignore': '🚫', '.gitattributes': '🚫',
    '.editorconfig': '⚙️', '.prettierrc': '⚙️', '.eslintrc': '⚙️',
    'package.json': '📦', 'package-lock.json': '🔒',
    'tsconfig.json': '📘', 'vite.config.js': '⚡', 'vite.config.ts': '⚡',
    'cargo.toml': '🦀', 'cargo.lock': '🔒',
    'go.mod': '🐹', 'go.sum': '🔒',
    'requirements.txt': '🐍', 'pyproject.toml': '🐍', 'setup.py': '🐍',
    'license': '📜', 'license.md': '📜', 'license.txt': '📜',
    'readme.md': '📖', 'readme': '📖', 'readme.txt': '📖',
    'changelog.md': '📋', 'changelog': '📋',
    '.env': '🔧', '.env.local': '🔧', '.env.production': '🔧',
  };
  if (specialNames[name]) return specialNames[name];

  // 扩展名匹配
  var ext = name.split('.').pop().toLowerCase();
  // 处理 .d.ts, .test.js 等复合扩展名
  if (name.endsWith('.d.ts')) return '📘';
  if (name.endsWith('.test.js') || name.endsWith('.spec.js')) return '🧪';
  if (name.endsWith('.test.ts') || name.endsWith('.spec.ts')) return '🧪';

  return FILE_EXT_ICONS[ext] || '📄';
}

/**
 * 通过语言ID获取图标emoji
 * @param {string} langId - 语言ID（如 'python', 'cpp'）
 * @returns {string} emoji图标
 */
function getLangIcon(langId) {
  if (!langId) return '📄';
  return LANG_ID_ICONS[langId] || '📄';
}
