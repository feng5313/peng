/**
 * {{ agent_name }} 登录页面 JavaScript
 *
 * 功能：
 * - 密码显示/隐藏切换
 * - 表单验证
 * - 登录提交处理
 * - 记住我功能
 * - 错误提示
 */

// ==================== 初始化 ====================

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initPasswordToggle();
    initLoginForm();
    loadRememberedUsername();
});

// ==================== 主题管理 ====================

function initTheme() {
    const savedTheme = localStorage.getItem('peng-theme') || 'dark';
    document.body.className = savedTheme === 'dark' ? 'dark-theme' : 'light-theme';
}

// ==================== 密码显示/隐藏 ====================

function initPasswordToggle() {
    const toggleBtn = document.getElementById('password-toggle');
    const passwordInput = document.getElementById('password');

    if (toggleBtn && passwordInput) {
        toggleBtn.addEventListener('click', () => {
            const isPassword = passwordInput.type === 'password';
            passwordInput.type = isPassword ? 'text' : 'password';
            toggleBtn.textContent = isPassword ? '🙈' : '👁️';
            toggleBtn.title = isPassword ? '隐藏密码' : '显示密码';
        });
    }
}

// ==================== 登录表单处理 ====================

function initLoginForm() {
    const form = document.getElementById('login-form');
    const loginBtn = document.getElementById('login-btn');
    const errorDiv = document.getElementById('login-error');

    if (form) {
        form.addEventListener('submit', async (e) => {
            e.preventDefault();

            // 获取表单数据
            const username = document.getElementById('username').value.trim();
            const password = document.getElementById('password').value;
            const rememberMe = document.getElementById('remember-me').checked;

            // 验证输入
            if (!validateForm(username, password, errorDiv)) {
                return;
            }

            // 显示加载状态
            setLoading(true, loginBtn);
            hideError(errorDiv);

            try {
                // 发送登录请求
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        username,
                        password,
                        remember_me: rememberMe,
                    }),
                });

                const data = await response.json();

                if (response.ok) {
                    // 登录成功
                    handleLoginSuccess(data, rememberMe, username);
                } else {
                    // 登录失败
                    handleLoginError(data.detail || '登录失败，请检查用户名和密码', errorDiv);
                }
            } catch (error) {
                console.error('登录请求失败:', error);
                handleLoginError('网络错误，请稍后重试', errorDiv);
            } finally {
                setLoading(false, loginBtn);
            }
        });
    }
}

// ==================== 表单验证 ====================

function validateForm(username, password, errorDiv) {
    // 验证用户名
    if (!username) {
        showError('请输入用户名', errorDiv);
        document.getElementById('username').focus();
        return false;
    }

    if (username.length < 3) {
        showError('用户名长度至少为 3 个字符', errorDiv);
        document.getElementById('username').focus();
        return false;
    }

    // 验证密码
    if (!password) {
        showError('请输入密码', errorDiv);
        document.getElementById('password').focus();
        return false;
    }

    if (password.length < 6) {
        showError('密码长度至少为 6 个字符', errorDiv);
        document.getElementById('password').focus();
        return false;
    }

    return true;
}

// ==================== 登录成功处理 ====================

function handleLoginSuccess(data, rememberMe, username) {
    // 如果选择了记住我，保存用户名
    if (rememberMe) {
        localStorage.setItem('peng-remembered-username', username);
    }

    // 显示成功消息
    showSuccess('登录成功，正在跳转...');

    // 延迟跳转到主页
    setTimeout(() => {
        // 如果有 token，保存到 localStorage
        if (data.token) {
            localStorage.setItem('peng-token', data.token);
        }

        // 跳转到主页
        window.location.href = '/';
    }, 1000);
}

// ==================== 登录失败处理 ====================

function handleLoginError(message, errorDiv) {
    showError(message, errorDiv);
}

// ==================== 记住我功能 ====================

function loadRememberedUsername() {
    const rememberedUsername = localStorage.getItem('peng-remembered-username');
    if (rememberedUsername) {
        const usernameInput = document.getElementById('username');
        const rememberMeCheckbox = document.getElementById('remember-me');

        if (usernameInput) {
            usernameInput.value = rememberedUsername;
        }

        if (rememberMeCheckbox) {
            rememberMeCheckbox.checked = true;
        }
    }
}

// ==================== UI 状态管理 ====================

function setLoading(loading, loginBtn) {
    const btnText = loginBtn.querySelector('.btn-text');
    const btnLoading = loginBtn.querySelector('.btn-loading');

    if (loading) {
        loginBtn.disabled = true;
        if (btnText) btnText.style.display = 'none';
        if (btnLoading) btnLoading.style.display = 'flex';
    } else {
        loginBtn.disabled = false;
        if (btnText) btnText.style.display = 'inline';
        if (btnLoading) btnLoading.style.display = 'none';
    }
}

function showError(message, errorDiv) {
    if (errorDiv) {
        errorDiv.textContent = message;
        errorDiv.style.display = 'block';
        errorDiv.className = 'error-message';
    }
}

function hideError(errorDiv) {
    if (errorDiv) {
        errorDiv.style.display = 'none';
        errorDiv.textContent = '';
    }
}

function showSuccess(message) {
    const errorDiv = document.getElementById('login-error');
    if (errorDiv) {
        errorDiv.textContent = message;
        errorDiv.style.display = 'block';
        errorDiv.className = 'success-message';
    }
}

// ==================== 键盘快捷键 ====================

// 支持在输入框中按 Enter 键提交表单（表单默认已支持）
// 支持按 Esc 键清空密码
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const passwordInput = document.getElementById('password');
        if (passwordInput && document.activeElement === passwordInput) {
            passwordInput.value = '';
        }
    }
});
