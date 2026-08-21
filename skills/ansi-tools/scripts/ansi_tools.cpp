/**
 * ANSI Tools - 统一ANSI文本处理工具 (C++ v3.1)
 *
 * 核心功能：
 * 1. get_text       - 获取文本及其位置和颜色信息
 * 2. get_screen     - 获取屏幕字符矩阵及颜色信息
 * 3. get_screen_text- 获取指定位置的连续文本
 * 4. plain          - 移除所有ANSI序列
 * 5. lines          - 基于光标定位渲染屏幕
 * 6. colored        - 保留颜色序列
 * 7. colors         - 分析颜色使用
 *
 * v3.1 更新:
 * - 新增 REP 控制码 (\x1b[#b) 支持
 *
 * 编译: g++ -std=c++17 -O2 -o ansi_tools ansi_tools.cpp
 * 使用: ./ansi_tools get_text < input.txt
 *       ./ansi_tools plain < input.txt
 */

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <algorithm>
#include <numeric>
#include <iomanip>
#include <memory>
#include <optional>
#include <regex>

// ========== 256色到RGB映射 ==========

std::string color256ToRgb[256];
std::map<int, std::string> basicColors;

void initColorTables() {
    // 0-15: 标准颜色
    const char* standardColors[16] = {
        "#000000", "#800000", "#008000", "#808000",
        "#000080", "#800080", "#008080", "#c0c0c0",
        "#808080", "#ff0000", "#00ff00", "#ffff00",
        "#0000ff", "#ff00ff", "#00ffff", "#ffffff"
    };
    for (int i = 0; i < 16; i++) {
        color256ToRgb[i] = standardColors[i];
    }

    // 16-231: 6x6x6颜色立方体
    for (int i = 16; i < 232; i++) {
        int n = i - 16;
        int r = (n / 36) * 51;
        int g = ((n % 36) / 6) * 51;
        int b = (n % 6) * 51;
        char buf[8];
        snprintf(buf, sizeof(buf), "#%02x%02x%02x", r, g, b);
        color256ToRgb[i] = buf;
    }

    // 232-255: 灰度渐变
    for (int i = 232; i < 256; i++) {
        int gray = 8 + (i - 232) * 10;
        char buf[8];
        snprintf(buf, sizeof(buf), "#%02x%02x%02x", gray, gray, gray);
        color256ToRgb[i] = buf;
    }

    // 基本颜色映射
    basicColors = {
        {30, "#000000"}, {31, "#800000"}, {32, "#008000"}, {33, "#808000"},
        {34, "#000080"}, {35, "#800080"}, {36, "#008080"}, {37, "#c0c0c0"},
        {40, "#000000"}, {41, "#800000"}, {42, "#008000"}, {43, "#808000"},
        {44, "#000080"}, {45, "#800080"}, {46, "#008080"}, {47, "#c0c0c0"},
        {90, "#808080"}, {91, "#ff0000"}, {92, "#00ff00"}, {93, "#ffff00"},
        {94, "#0000ff"}, {95, "#ff00ff"}, {96, "#00ffff"}, {97, "#ffffff"},
        {100, "#808080"}, {101, "#ff0000"}, {102, "#00ff00"}, {103, "#ffff00"},
        {104, "#0000ff"}, {105, "#ff00ff"}, {106, "#00ffff"}, {107, "#ffffff"}
    };
}

// ========== UTF-8工具函数 ==========

// 获取UTF-8字符的字节长度
int utf8CharLen(unsigned char c) {
    if ((c & 0x80) == 0) return 1;        // ASCII
    if ((c & 0xE0) == 0xC0) return 2;     // 2字节
    if ((c & 0xF0) == 0xE0) return 3;     // 3字节
    if ((c & 0xF8) == 0xF0) return 4;     // 4字节
    return 1;
}

// 获取UTF-8字符的Unicode码点
uint32_t getUnicodeCodepoint(const std::string& s, size_t pos) {
    if (pos >= s.length()) return 0;
    unsigned char c = s[pos];

    if ((c & 0x80) == 0) return c;
    if ((c & 0xE0) == 0xC0) {
        if (pos + 1 >= s.length()) return c;
        return ((c & 0x1F) << 6) | (s[pos+1] & 0x3F);
    }
    if ((c & 0xF0) == 0xE0) {
        if (pos + 2 >= s.length()) return c;
        return ((c & 0x0F) << 12) | ((s[pos+1] & 0x3F) << 6) | (s[pos+2] & 0x3F);
    }
    if ((c & 0xF8) == 0xF0) {
        if (pos + 3 >= s.length()) return c;
        return ((c & 0x07) << 18) | ((s[pos+1] & 0x3F) << 12) |
               ((s[pos+2] & 0x3F) << 6) | (s[pos+3] & 0x3F);
    }
    return c;
}

// 判断是否为双宽度字符
bool isWideChar(uint32_t cp) {
    // CJK统一汉字
    if (cp >= 0x4E00 && cp <= 0x9FFF) return true;
    // CJK扩展A
    if (cp >= 0x3400 && cp <= 0x4DBF) return true;
    // CJK兼容汉字
    if (cp >= 0xF900 && cp <= 0xFAFF) return true;
    // CJK标点符号
    if (cp >= 0x3000 && cp <= 0x303F) return true;
    // 全角ASCII、全角标点
    if (cp >= 0xFF00 && cp <= 0xFFEF) return true;
    // Emoji
    if (cp >= 0x1F600 && cp <= 0x1F64F) return true;
    if (cp >= 0x1F300 && cp <= 0x1F5FF) return true;
    if (cp >= 0x1F680 && cp <= 0x1F6FF) return true;
    if (cp >= 0x1F1E0 && cp <= 0x1F1FF) return true;
    if (cp >= 0x2600 && cp <= 0x26FF) return true;
    if (cp >= 0x2700 && cp <= 0x27BF) return true;
    // 制表符、方块、几何图形
    if (cp >= 0x2500 && cp <= 0x257F) return true;
    if (cp >= 0x2580 && cp <= 0x259F) return true;
    if (cp >= 0x25A0 && cp <= 0x25FF) return true;
    return false;
}

int getCharWidth(uint32_t cp) {
    return isWideChar(cp) ? 2 : 1;
}

// ========== AnsiStyle类 ==========

struct AnsiStyle {
    std::optional<int> fgColor;   // 256色
    std::optional<int> bgRgb;     // RGB打包值
    std::optional<int> fgBasic;
    std::optional<int> bgColor;
    std::optional<int> bgRgbFg;
    std::optional<int> bgBasic;
    bool bold = false;
    bool dim = false;
    bool italic = false;
    bool underline = false;
    bool blink = false;
    bool reverse = false;
    bool hidden = false;
    bool strikethrough = false;

    void reset() {
        fgColor.reset(); bgRgb.reset(); fgBasic.reset();
        bgColor.reset(); bgRgbFg.reset(); bgBasic.reset();
        bold = dim = italic = underline = blink = false;
        reverse = hidden = strikethrough = false;
    }

    std::string getFgHex() const {
        if (reverse) return getBgHexInternal();
        return getFgHexInternal();
    }

    std::string getBgHex() const {
        if (reverse) return getFgHexInternal();
        return getBgHexInternal();
    }

    std::string getFgHexInternal() const {
        if (fgColor.has_value()) {
            return color256ToRgb[fgColor.value() % 256];
        }
        if (bgRgb.has_value()) {
            int v = bgRgb.value();
            char buf[8];
            snprintf(buf, sizeof(buf), "#%02x%02x%02x", (v>>16)&0xFF, (v>>8)&0xFF, v&0xFF);
            return buf;
        }
        if (fgBasic.has_value()) {
            auto it = basicColors.find(fgBasic.value());
            if (it != basicColors.end()) return it->second;
        }
        return "";
    }

    std::string getBgHexInternal() const {
        if (bgColor.has_value()) {
            return color256ToRgb[bgColor.value() % 256];
        }
        if (bgRgbFg.has_value()) {
            int v = bgRgbFg.value();
            char buf[8];
            snprintf(buf, sizeof(buf), "#%02x%02x%02x", (v>>16)&0xFF, (v>>8)&0xFF, v&0xFF);
            return buf;
        }
        if (bgBasic.has_value()) {
            auto it = basicColors.find(bgBasic.value());
            if (it != basicColors.end()) return it->second;
        }
        return "";
    }

    std::string toAnsiCode() const {
        std::vector<std::string> codes;
        if (bold) codes.push_back("1");
        if (dim) codes.push_back("2");
        if (italic) codes.push_back("3");
        if (underline) codes.push_back("4");
        if (blink) codes.push_back("5");
        if (reverse) codes.push_back("7");
        if (hidden) codes.push_back("8");
        if (strikethrough) codes.push_back("9");

        if (fgBasic.has_value()) {
            codes.push_back(std::to_string(fgBasic.value()));
        } else if (fgColor.has_value()) {
            codes.push_back("38;5;" + std::to_string(fgColor.value()));
        } else if (bgRgb.has_value()) {
            int v = bgRgb.value();
            codes.push_back("38;2;" + std::to_string((v>>16)&0xFF) + ";" +
                          std::to_string((v>>8)&0xFF) + ";" + std::to_string(v&0xFF));
        }

        if (bgBasic.has_value()) {
            codes.push_back(std::to_string(bgBasic.value()));
        } else if (bgColor.has_value()) {
            codes.push_back("48;5;" + std::to_string(bgColor.value()));
        } else if (bgRgbFg.has_value()) {
            int v = bgRgbFg.value();
            codes.push_back("48;2;" + std::to_string((v>>16)&0xFF) + ";" +
                          std::to_string((v>>8)&0xFF) + ";" + std::to_string(v&0xFF));
        }

        if (codes.empty()) return "";
        std::string result = "\x1b[";
        for (size_t i = 0; i < codes.size(); i++) {
            if (i > 0) result += ";";
            result += codes[i];
        }
        return result + "m";
    }

    AnsiStyle copy() const { return *this; }
};

// ========== TerminalState类 ==========

struct TerminalState {
    int row = 0, col = 0;
    AnsiStyle style;
    int savedRow = 0, savedCol = 0;
    AnsiStyle savedStyle;

    void saveCursor() {
        savedRow = row; savedCol = col;
        savedStyle = style.copy();
    }

    void restoreCursor() {
        row = savedRow; col = savedCol;
        style = savedStyle;
    }

    void reset() {
        row = col = savedRow = savedCol = 0;
        style.reset(); savedStyle.reset();
    }
};

// ========== JSON辅助函数 ==========

std::string escapeJson(const std::string& s) {
    std::string result;
    for (char c : s) {
        switch (c) {
            case '"': result += "\\\""; break;
            case '\\': result += "\\\\"; break;
            case '\b': result += "\\b"; break;
            case '\f': result += "\\f"; break;
            case '\n': result += "\\n"; break;
            case '\r': result += "\\r"; break;
            case '\t': result += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned char>(c));
                    result += buf;
                } else {
                    result += c;
                }
        }
    }
    return result;
}

// ========== CSI参数解析 ==========

std::vector<std::optional<int>> parseCsiParams(const std::string& seq) {
    std::vector<std::optional<int>> params;
    std::string s = seq;
    if (!s.empty() && s[0] == '?') s = s.substr(1);

    size_t start = 0;
    while (start <= s.length()) {
        size_t pos = s.find(';', start);
        if (pos == std::string::npos) pos = s.length();

        std::string part = s.substr(start, pos - start);
        // trim
        size_t first = part.find_first_not_of(" \t");
        if (first == std::string::npos) {
            params.push_back(std::nullopt);
        } else {
            try {
                params.push_back(std::stoi(part.substr(first)));
            } catch (...) {
                params.push_back(std::nullopt);
            }
        }
        start = pos + 1;
    }
    return params;
}

// ========== SGR参数解析 ==========

void parseSgrParams(const std::vector<std::optional<int>>& params, AnsiStyle& style) {
    for (size_t i = 0; i < params.size(); i++) {
        auto p = params[i];
        if (!p.has_value() || p.value() == 0) {
            style.reset();
        } else if (p.value() == 1) {
            style.bold = true;
        } else if (p.value() == 2) {
            style.dim = true;
        } else if (p.value() == 3) {
            style.italic = true;
        } else if (p.value() == 4) {
            style.underline = true;
        } else if (p.value() == 5 || p.value() == 6) {
            style.blink = true;
        } else if (p.value() == 7) {
            style.reverse = true;
        } else if (p.value() == 8) {
            style.hidden = true;
        } else if (p.value() == 9) {
            style.strikethrough = true;
        } else if (p.value() >= 30 && p.value() <= 37) {
            style.fgBasic = p.value();
            style.fgColor.reset(); style.bgRgb.reset();
        } else if (p.value() >= 90 && p.value() <= 97) {
            style.fgBasic = p.value();
            style.fgColor.reset(); style.bgRgb.reset();
        } else if (p.value() == 38) {
            if (i + 2 < params.size() && params[i+1].has_value() && params[i+1].value() == 5) {
                style.fgColor = params[i+2];
                style.fgBasic.reset(); style.bgRgb.reset();
                i += 2;
            } else if (i + 4 < params.size() && params[i+1].has_value() && params[i+1].value() == 2) {
                int r = params[i+2].value_or(0);
                int g = params[i+3].value_or(0);
                int b = params[i+4].value_or(0);
                style.bgRgb = (r << 16) | (g << 8) | b;
                style.fgBasic.reset(); style.fgColor.reset();
                i += 4;
            }
        } else if (p.value() >= 40 && p.value() <= 47) {
            style.bgBasic = p.value();
            style.bgColor.reset(); style.bgRgbFg.reset();
        } else if (p.value() >= 100 && p.value() <= 107) {
            style.bgBasic = p.value();
            style.bgColor.reset(); style.bgRgbFg.reset();
        } else if (p.value() == 48) {
            if (i + 2 < params.size() && params[i+1].has_value() && params[i+1].value() == 5) {
                style.bgColor = params[i+2];
                style.bgBasic.reset(); style.bgRgbFg.reset();
                i += 2;
            } else if (i + 4 < params.size() && params[i+1].has_value() && params[i+1].value() == 2) {
                int r = params[i+2].value_or(0);
                int g = params[i+3].value_or(0);
                int b = params[i+4].value_or(0);
                style.bgRgbFg = (r << 16) | (g << 8) | b;
                style.bgBasic.reset(); style.bgColor.reset();
                i += 4;
            }
        } else if (p.value() == 39) {
            style.fgBasic.reset(); style.fgColor.reset(); style.bgRgb.reset();
        } else if (p.value() == 49) {
            style.bgBasic.reset(); style.bgColor.reset(); style.bgRgbFg.reset();
        }
    }
}

// ========== 屏幕矩阵项 ==========

struct ScreenItem {
    std::string ch;
    AnsiStyle style;
    bool isNone = true;

    ScreenItem() : isNone(true) {}
    ScreenItem(const std::string& c, const AnsiStyle& s) : ch(c), style(s), isNone(false) {}
};

// ========== 核心解析函数 ==========

struct ParseResult {
    TerminalState state;
    std::vector<std::vector<ScreenItem>> screen;
    std::vector<std::tuple<std::string, int, int, AnsiStyle>> allChars;
};

ParseResult parseAnsiToScreen(const std::string& text, int width, int height) {
    ParseResult result;
    result.screen.assign(height, std::vector<ScreenItem>(width));
    std::string lastChar = " ";  // 用于 REP 控制码：记录上一个打印的字符

    size_t i = 0;
    size_t n = text.length();

    while (i < n) {
        unsigned char ch = text[i];

        // 处理ANSI转义序列
        if (ch == 0x1b && i + 1 < n) {
            unsigned char next = text[i + 1];

            if (next == '[') {
                // CSI序列
                size_t j = i + 2;
                while (j < n && (text[j] < 0x40 || text[j] > 0x7E)) j++;

                if (j >= n) { i++; continue; }

                std::string seq = text.substr(i + 2, j - i - 2);
                char cmd = text[j];
                auto params = parseCsiParams(seq);

                auto& state = result.state;
                auto& screen = result.screen;

                if (cmd == 'H' || cmd == 'f') {
                    int rowVal = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    int colVal = params.size() > 1 && params[1].has_value() ? params[1].value() : 1;
                    state.row = std::max(0, std::min(rowVal - 1, height - 1));
                    state.col = std::max(0, std::min(colVal - 1, width - 1));
                } else if (cmd == 'A') {
                    int mv = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    state.row = std::max(0, state.row - mv);
                } else if (cmd == 'B') {
                    int mv = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    state.row = std::min(height - 1, state.row + mv);
                } else if (cmd == 'C') {
                    int mv = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    state.col = std::min(width - 1, state.col + mv);
                } else if (cmd == 'D') {
                    int mv = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    state.col = std::max(0, state.col - mv);
                } else if (cmd == 'G') {
                    int mv = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    state.col = std::max(0, std::min(mv - 1, width - 1));
                } else if (cmd == 'd') {
                    int mv = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    state.row = std::max(0, std::min(mv - 1, height - 1));
                } else if (cmd == 's') {
                    state.saveCursor();
                } else if (cmd == 'u') {
                    state.restoreCursor();
                } else if (cmd == 'm') {
                    parseSgrParams(params, state.style);
                } else if (cmd == 'J') {
                    int mode = params.size() > 0 && params[0].has_value() ? params[0].value() : 0;
                    if (mode == 2 || mode == 3) {
                        screen.assign(height, std::vector<ScreenItem>(width));
                    }
                } else if (cmd == 'K') {
                    int mode = params.size() > 0 && params[0].has_value() ? params[0].value() : 0;
                    if (mode == 0) {
                        for (int c = state.col; c < width; c++)
                            screen[state.row][c] = ScreenItem();
                    } else if (mode == 2) {
                        for (int c = 0; c < width; c++)
                            screen[state.row][c] = ScreenItem();
                    }
                } else if (cmd == 'b') {
                    // REP 控制码：重复上一个字符
                    int count = params.size() > 0 && params[0].has_value() ? params[0].value() : 1;
                    for (int r = 0; r < count; r++) {
                        int curCol = state.col;
                        int curRow = state.row;
                        uint32_t cp = getUnicodeCodepoint(lastChar, 0);
                        int charWidth = getCharWidth(cp);

                        if (curRow >= 0 && curRow < height && curCol >= 0 && curCol < width) {
                            result.allChars.push_back({lastChar, curCol, curRow, state.style.copy()});
                            screen[curRow][curCol] = ScreenItem(lastChar, state.style);
                            if (charWidth == 2 && curCol + 1 < width) {
                                screen[curRow][curCol + 1] = ScreenItem("__WIDE__", state.style);
                            }
                        }
                        state.col += charWidth;
                        if (state.col > width) state.col = width;
                    }
                }

                i = j + 1;
                continue;
            } else if (next == '7') {
                result.state.saveCursor();
                i += 2; continue;
            } else if (next == '8') {
                result.state.restoreCursor();
                i += 2; continue;
            } else if (next == 'c') {
                result.state.reset();
                result.screen.assign(height, std::vector<ScreenItem>(width));
                i += 2; continue;
            } else if (next == 'M') {
                result.state.row = std::max(0, result.state.row - 1);
                i += 2; continue;
            } else if (next == 'D') {
                result.state.row = std::min(height - 1, result.state.row + 1);
                i += 2; continue;
            } else if (next == 'E') {
                result.state.row = std::min(height - 1, result.state.row + 1);
                result.state.col = 0;
                i += 2; continue;
            } else if (next == ']' || next == 'P' || next == '_' || next == 'X' || next == '^') {
                // 跳过OSC/DCS等序列
                size_t end = i + 2;
                while (end < n && text[end] != 0x07 && !(text[end] == 0x1b && end + 1 < n && text[end+1] == '\\'))
                    end++;
                if (end < n && text[end] == 0x07) end++;
                else if (end < n) end += 2;
                i = end; continue;
            } else if (next == '(' || next == ')') {
                i += 3; continue;
            } else {
                i += 2; continue;
            }
        }

        // 处理控制字符
        auto& state = result.state;
        auto& screen = result.screen;

        if (ch == '\r') {
            state.col = 0;
        } else if (ch == '\n') {
            state.row = std::min(height - 1, state.row + 1);
            state.col = 0;
        } else if (ch == '\t') {
            int nextCol = std::min(((state.col / 8) + 1) * 8, width);
            while (state.col < nextCol) {
                if (state.row >= 0 && state.row < height) {
                    screen[state.row][state.col] = ScreenItem(" ", state.style);
                }
                state.col++;
            }
        } else if (ch == '\b') {
            state.col = std::max(0, state.col - 1);
        } else if (ch >= 32 || ch < 0x80) {
            // 获取UTF-8字符
            int charLen = (ch & 0x80) ? utf8CharLen(ch) : 1;
            std::string utf8Char = text.substr(i, charLen);
            uint32_t cp = getUnicodeCodepoint(text, i);
            int charWidth = getCharWidth(cp);
            lastChar = utf8Char;  // 更新最后一个字符，供 REP 控制码使用

            int curCol = state.col;
            int curRow = state.row;

            if (curRow >= 0 && curRow < height && curCol >= 0 && curCol < width) {
                result.allChars.push_back({utf8Char, curCol, curRow, state.style.copy()});
                screen[curRow][curCol] = ScreenItem(utf8Char, state.style);

                if (charWidth == 2 && curCol + 1 < width) {
                    screen[curRow][curCol + 1] = ScreenItem("__WIDE__", state.style);
                }
            }

            state.col += charWidth;
            if (state.col > width) state.col = width;
            i += charLen - 1;  // for循环会+1
        }

        i++;
    }

    return result;
}

// ========== 公共API函数 ==========

std::string getText(const std::string& text, int width = 80, int height = 24) {
    auto result = parseAnsiToScreen(text, width, height);

    std::string textStr;
    std::vector<std::string> positions;
    std::vector<std::string> colorPairs;
    std::set<std::string> fgColors, bgColors;

    for (const auto& [ch, col, row, style] : result.allChars) {
        textStr += ch;
        positions.push_back("[" + std::to_string(col) + "," + std::to_string(row) + "]");
        std::string fg = style.getFgHex();
        std::string bg = style.getBgHex();
        colorPairs.push_back("[\"" + escapeJson(bg) + "\",\"" + escapeJson(fg) + "\"]");
        if (!fg.empty()) fgColors.insert(fg);
        if (!bg.empty()) bgColors.insert(bg);
    }

    // 构建text_lines和colored_lines
    std::vector<std::string> textLines, coloredLines;

    for (const auto& row : result.screen) {
        std::string textLine, coloredLine;
        AnsiStyle* prevStyle = nullptr;
        std::string prevAnsiCode;

        for (const auto& item : row) {
            if (!item.isNone) {
                if (item.ch == "__WIDE__") continue;
                textLine += item.ch;
                std::string ansiCode = item.style.toAnsiCode();
                if (!prevStyle || ansiCode != prevAnsiCode) {
                    coloredLine += ansiCode;
                }
                coloredLine += item.ch;
                prevStyle = const_cast<AnsiStyle*>(&item.style);
                prevAnsiCode = ansiCode;
            } else {
                textLine += " ";
                if (prevStyle && !prevAnsiCode.empty()) {
                    coloredLine += "\x1b[0m";
                    prevStyle = nullptr;
                    prevAnsiCode.clear();
                }
                coloredLine += " ";
            }
        }

        if (prevStyle && !prevAnsiCode.empty()) {
            coloredLine += "\x1b[0m";
        }

        // rtrim
        while (!textLine.empty() && (textLine.back() == ' ' || textLine.back() == '\t'))
            textLine.pop_back();
        while (!coloredLine.empty() && (coloredLine.back() == ' ' || coloredLine.back() == '\t'))
            coloredLine.pop_back();

        if (!coloredLine.empty() && coloredLine.substr(coloredLine.length()-4) != "\x1b[0m") {
            coloredLine += "\x1b[0m";
        }

        textLines.push_back(textLine);
        coloredLines.push_back(coloredLine);
    }

    // 去除末尾空行
    while (textLines.size() > 1 && textLines.back().empty()) {
        textLines.pop_back();
        coloredLines.pop_back();
    }

    // 构建JSON
    std::string json = "{\n";
    json += "  \"text\": \"" + escapeJson(textStr) + "\",\n";
    json += "  \"positions\": [";
    for (size_t i = 0; i < positions.size(); i++) {
        if (i > 0) json += ",";
        json += positions[i];
    }
    json += "],\n";
    json += "  \"colors\": [";
    for (size_t i = 0; i < colorPairs.size(); i++) {
        if (i > 0) json += ",";
        json += colorPairs[i];
    }
    json += "],\n";
    json += "  \"text_lines\": \"" + escapeJson(std::accumulate(textLines.begin(), textLines.end(), std::string(),
        [](const std::string& a, const std::string& b) { return a.empty() ? b : a + "\\n" + b; })) + "\",\n";
    json += "  \"colored_lines\": \"" + escapeJson(std::accumulate(coloredLines.begin(), coloredLines.end(), std::string(),
        [](const std::string& a, const std::string& b) { return a.empty() ? b : a + "\\n" + b; })) + "\",\n";

    // color_set
    json += "  \"color_set\": {\"foreground\": [";
    bool first = true;
    for (const auto& c : fgColors) {
        if (!first) json += ","; first = false;
        json += "\"" + c + "\"";
    }
    json += "], \"background\": [";
    first = true;
    for (const auto& c : bgColors) {
        if (!first) json += ","; first = false;
        json += "\"" + c + "\"";
    }
    json += "]},\n";

    // cursor
    json += "  \"cursor\": {\"position\": [" + std::to_string(result.state.col) + "," +
            std::to_string(result.state.row) + "], \"style\": {\"fg\": \"" +
            result.state.style.getFgHex() + "\", \"bg\": \"" + result.state.style.getBgHex() +
            "\", \"bold\": " + (result.state.style.bold ? "true" : "false") +
            ", \"italic\": " + (result.state.style.italic ? "true" : "false") +
            ", \"underline\": " + (result.state.style.underline ? "true" : "false") + "}}}\n";

    return json;
}

std::string getScreen(const std::string& text, int width = 80, int height = 24) {
    auto result = parseAnsiToScreen(text, width, height);

    std::string json = "{\n  \"chars\": [\n";
    std::set<std::string> fgColors, bgColors;

    for (size_t r = 0; r < result.screen.size(); r++) {
        if (r > 0) json += ",\n";
        json += "    [";
        std::string prevChar;
        std::string prevBg, prevFg;

        for (size_t c = 0; c < result.screen[r].size(); c++) {
            if (c > 0) json += ",";
            const auto& item = result.screen[r][c];

            if (item.isNone) {
                json += "\"\"";
            } else if (item.ch == "__WIDE__") {
                json += "\"" + escapeJson(prevChar + "r") + "\"";
            } else {
                uint32_t cp = getUnicodeCodepoint(item.ch, 0);
                int cw = getCharWidth(cp);
                if (cw == 2) {
                    json += "\"" + escapeJson(item.ch + "l") + "\"";
                } else {
                    json += "\"" + escapeJson(item.ch) + "\"";
                }
                prevChar = item.ch;
                prevBg = item.style.getBgHex();
                prevFg = item.style.getFgHex();
                if (!prevFg.empty()) fgColors.insert(prevFg);
                if (!prevBg.empty()) bgColors.insert(prevBg);
            }
        }
        json += "]";
    }

    json += "\n  ],\n  \"colors\": [\n";
    // 简化：省略colors数组的详细输出，与chars对应
    for (size_t r = 0; r < result.screen.size(); r++) {
        if (r > 0) json += ",\n";
        json += "    [";
        for (size_t c = 0; c < result.screen[r].size(); c++) {
            if (c > 0) json += ",";
            const auto& item = result.screen[r][c];
            if (item.isNone) {
                json += "[\"\",\"\"]";
            } else {
                json += "[\"" + item.style.getBgHex() + "\",\"" + item.style.getFgHex() + "\"]";
            }
        }
        json += "]";
    }

    // color_set
    json += "\n  ],\n  \"color_set\": {\"foreground\": [";
    bool first = true;
    for (const auto& c : fgColors) {
        if (!first) json += ","; first = false;
        json += "\"" + c + "\"";
    }
    json += "], \"background\": [";
    first = true;
    for (const auto& c : bgColors) {
        if (!first) json += ","; first = false;
        json += "\"" + c + "\"";
    }
    json += "]},\n";

    json += "  \"cursor\": {\"position\": [" + std::to_string(result.state.col) + "," +
            std::to_string(result.state.row) + "]}}\n";

    return json;
}

std::string pureText(const std::string& text) {
    auto result = parseAnsiToScreen(text, 80, 24);
    std::string s;
    for (const auto& [ch, col, row, style] : result.allChars) {
        s += ch;
    }
    if (!s.empty()) s += "\n";
    return s;
}

std::string textLines(const std::string& text, int width = 80, int height = 24) {
    auto result = parseAnsiToScreen(text, width, height);
    std::vector<std::string> lines;

    for (const auto& row : result.screen) {
        std::string line;
        for (const auto& item : row) {
            if (item.isNone) {
                line += " ";
            } else if (item.ch == "__WIDE__") {
                // 双宽度字符的第二个位置，跳过（字符已在第一个位置输出）
                continue;
            } else {
                line += item.ch;
            }
        }
        // rtrim
        while (!line.empty() && (line.back() == ' ' || line.back() == '\t'))
            line.pop_back();
        lines.push_back(line);
    }

    while (lines.size() > 1 && lines.back().empty())
        lines.pop_back();

    std::string result_str;
    for (size_t i = 0; i < lines.size(); i++) {
        if (i > 0) result_str += "\n";
        result_str += lines[i];
    }
    if (!result_str.empty()) result_str += "\n";
    return result_str;
}

std::string coloredLines(const std::string& text, int width = 80, int height = 24) {
    auto result = parseAnsiToScreen(text, width, height);
    std::vector<std::string> lines;

    for (const auto& row : result.screen) {
        std::string line;
        AnsiStyle* prevStyle = nullptr;
        std::string prevAnsiCode;

        for (const auto& item : row) {
            if (item.isNone) {
                if (prevStyle && !prevAnsiCode.empty()) {
                    line += "\x1b[0m";
                    prevStyle = nullptr;
                    prevAnsiCode.clear();
                }
                line += " ";
            } else if (item.ch == "__WIDE__") {
                continue;
            } else {
                std::string ansiCode = item.style.toAnsiCode();
                if (!prevStyle || ansiCode != prevAnsiCode) {
                    line += ansiCode;
                }
                line += item.ch;
                prevStyle = const_cast<AnsiStyle*>(&item.style);
                prevAnsiCode = ansiCode;
            }
        }

        if (prevStyle && !prevAnsiCode.empty()) {
            line += "\x1b[0m";
        }

        while (!line.empty() && (line.back() == ' ' || line.back() == '\t'))
            line.pop_back();

        if (!line.empty() && line.substr(line.length()-4) != "\x1b[0m") {
            line += "\x1b[0m";
        }

        lines.push_back(line);
    }

    // 移除末尾的空行和只有重置码的行
    while (lines.size() > 1 && (lines.back().empty() || lines.back() == "\x1b[0m"))
        lines.pop_back();

    std::string result_str;
    for (size_t i = 0; i < lines.size(); i++) {
        if (i > 0) result_str += "\n";
        result_str += lines[i];
    }
    if (!result_str.empty()) result_str += "\n";
    return result_str;
}

std::string analyzeColors(const std::string& text, int width = 80, int height = 24) {
    auto result = parseAnsiToScreen(text, width, height);
    std::set<std::string> fgColors, bgColors;

    for (const auto& [ch, col, row, style] : result.allChars) {
        std::string fg = style.getFgHex();
        std::string bg = style.getBgHex();
        if (!fg.empty()) fgColors.insert(fg);
        if (!bg.empty()) bgColors.insert(bg);
    }

    std::string json = "{\n  \"foreground\": [";
    bool first = true;
    for (const auto& c : fgColors) {
        if (!first) json += ","; first = false;
        json += "\"" + c + "\"";
    }
    json += "],\n  \"background\": [";
    first = true;
    for (const auto& c : bgColors) {
        if (!first) json += ","; first = false;
        json += "\"" + c + "\"";
    }
    json += "],\n  \"cursor\": {\"position\": [" + std::to_string(result.state.col) + "," +
            std::to_string(result.state.row) + "]}}\n";
    return json;
}

// ========== main函数 ==========

void printUsage(const char* prog) {
    std::cerr << "usage: " << prog << " [-h] [-o OUTPUT] [--width WIDTH] [--height HEIGHT]\n"
              << "                     <command> [input_file]\n\n"
              << "ANSI工具集 v3.1 (C++)\n\n"
              << "positional arguments:\n"
              << "  command              输出格式\n"
              << "                       {get_text,get_screen,plain,lines,colored,colors}\n"
              << "  input_file           输入文件\n\n"
              << "options:\n"
              << "  -h, --help           show this help message and exit\n"
              << "  -o OUTPUT, --output OUTPUT\n"
              << "                       输出文件\n"
              << "  --width WIDTH        屏幕宽度\n"
              << "  --height HEIGHT      屏幕高度\n\n"
              << "使用示例：\n"
              << "  # 获取文本及位置颜色信息\n"
              << "  " << prog << " get_text < input.txt\n\n"
              << "  # 获取屏幕字符矩阵\n"
              << "  " << prog << " get_screen < input.txt\n\n"
              << "  # 其他功能\n"
              << "  " << prog << " plain < input.txt\n"
              << "  " << prog << " lines < input.txt\n"
              << "  " << prog << " colored < input.txt\n"
              << "  " << prog << " colors < input.txt\n";
}

int main(int argc, char* argv[]) {
    initColorTables();

    // 检查 -h 或 --help
    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            printUsage(argv[0]);
            return 0;
        }
    }

    if (argc < 2) {
        printUsage(argv[0]);
        return 1;
    }

    std::string format = argv[1];
    int width = 80, height = 24;
    std::string outputFile;
    std::string inputFile;

    for (int i = 2; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "--width" && i + 1 < argc) {
            width = std::stoi(argv[++i]);
        } else if (arg == "--height" && i + 1 < argc) {
            height = std::stoi(argv[++i]);
        } else if (arg.substr(0, 7) == "--width" && arg.find('=') != std::string::npos) {
            width = std::stoi(arg.substr(arg.find('=') + 1));
        } else if (arg.substr(0, 8) == "--height" && arg.find('=') != std::string::npos) {
            height = std::stoi(arg.substr(arg.find('=') + 1));
        } else if (arg == "-o" && i + 1 < argc) {
            outputFile = argv[++i];
        } else if (arg == "--output" && i + 1 < argc) {
            outputFile = argv[++i];
        } else if (arg[0] != '-') {
            inputFile = arg;
        }
    }

    // 读取输入（从文件或stdin）
    std::string input;
    if (!inputFile.empty()) {
        std::ifstream fin(inputFile);
        if (!fin) {
            std::cerr << "无法打开文件: " << inputFile << "\n";
            return 1;
        }
        input = std::string((std::istreambuf_iterator<char>(fin)),
                            std::istreambuf_iterator<char>());
    } else {
        input = std::string((std::istreambuf_iterator<char>(std::cin)),
                            std::istreambuf_iterator<char>());
    }

    std::string output;

    if (format == "get_text") {
        output = getText(input, width, height);
    } else if (format == "get_screen") {
        output = getScreen(input, width, height);
    } else if (format == "plain") {
        output = pureText(input);
    } else if (format == "lines") {
        output = textLines(input, width, height);
    } else if (format == "colored") {
        output = coloredLines(input, width, height);
    } else if (format == "colors") {
        output = analyzeColors(input, width, height);
    } else {
        std::cerr << "未知命令: " << format << "\n";
        printUsage(argv[0]);
        return 1;
    }

    if (!outputFile.empty()) {
        std::ofstream fout(outputFile);
        fout << output;
    } else {
        std::cout << output;
    }

    return 0;
}
