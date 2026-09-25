let currentConvId = null;
let currentAudio = null;
let currentAudioButton = null;
let isProcessing = false;
let audioPollInterval = null;

// Initialize App & Check Theme
window.onload = () => {
    checkAuth();
    // Light is default, only add dark-theme if specifically stored
    if (localStorage.getItem('theme') === 'dark') {
        document.body.classList.add('dark-theme');
    }
};

// Listen for clicks to close modals or dropdown menus
document.addEventListener('click', (e) => {
    // Close context menu if clicked outside
    if (!e.target.closest('.dots-btn') && !e.target.closest('.context-menu')) {
        document.querySelectorAll('.context-menu').forEach(m => m.classList.remove('show'));
    }
    
    // Close Logout modal if user clicks on the dark overlay (outside the box)
    if (e.target.id === 'logout-modal') {
        closeLogoutModal();
    }
});

async function checkAuth() {
    const res = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (res.ok) {
        document.getElementById('auth-overlay').style.display = 'none';
        initApp();
    }
}

function checkAndPollAudio() {
    if (audioPollInterval) clearInterval(audioPollInterval);

    // If there is no loading button, do nothing
    const pendingBtn = document.querySelector('.audio-control-btn.loading');
    if (!pendingBtn) return;

    // Ping the backend every 3 seconds to see if the audio URL is ready
    audioPollInterval = setInterval(async () => {
        try {
            const res = await fetch(`/api/conversations/${currentConvId}/messages`);
            const messages = await res.json();
            
            // Look at the last message
            const lastMsg = messages[messages.length - 1];
            
            if (lastMsg && lastMsg.role === 'assistant' && 
                !lastMsg.audio_url.includes('pending') && 
                !lastMsg.audio_url.includes('processing')) {
                
                clearInterval(audioPollInterval);
                
                // Update the button UI instantly
                const btn = document.querySelector('.audio-control-btn.loading');
                if (btn) {
                    btn.className = 'audio-control-btn';
                    btn.innerHTML = '▶ Play Voice';
                    btn.disabled = false;
                    btn.onclick = () => handleAudioToggle(btn, lastMsg.audio_url, lastMsg.audio_url_2);
                }
            }
        } catch (err) {
            console.error("Audio polling error:", err);
        }
    }, 3000);
}

async function submitAuth() {
    const u = document.getElementById('auth-user').value.trim();
    const p = document.getElementById('auth-pass').value.trim();
    if (!u || !p) return alert('Username and Password required');

    const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username: u, password: p })
    });

    const data = await res.json();

    if (res.ok) {
        if (data.status === 'requires_password_change') {
            document.getElementById('login-view').style.display = 'none';
            document.getElementById('reset-view').style.display = 'block';
            sessionStorage.setItem('temp_username', u);
            sessionStorage.setItem('temp_password', p);
        } else if (data.status === 'success') {
            document.getElementById('auth-overlay').style.display = 'none';
            initApp();
        }
    } else {
        alert(data.detail || 'Authentication failed');
    }
}

async function submitPasswordReset() {
    const username = sessionStorage.getItem('temp_username');
    const old_password = sessionStorage.getItem('temp_password');
    const new_password = document.getElementById('reset-new-pass').value.trim();

    if (!new_password) return alert('Please enter a new password');

    const res = await fetch('/api/auth/change_password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username, old_password, new_password })
    });

    const data = await res.json();

    if (res.ok && data.status === 'success') {
        sessionStorage.removeItem('temp_username');
        sessionStorage.removeItem('temp_password');
        document.getElementById('auth-overlay').style.display = 'none';
        initApp();
    } else {
        alert(data.detail || 'Failed to update password.');
    }
}

async function initApp() {
    fetch('/api/warmup');
    await refreshSidebar();

    const res = await fetch('/api/conversations');
    const convs = await res.json();
    if (convs.length > 0) {
        loadConversation(convs[0].id, convs[0].title);
    } else {
        startNewChat();
    }
}

function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('hidden');
}

function closeSidebarOnMobile() {
    if (window.innerWidth <= 768) {
        document.getElementById('sidebar').classList.add('hidden');
    }
}

async function refreshSidebar() {
    const res = await fetch('/api/conversations');
    if (!res.ok) return promptLogout(); // if unauthorized, just prompt logout or handle natively

    const convs = await res.json();
    const list = document.getElementById('conversation-list');
    list.innerHTML = '';

    convs.forEach(c => {
        const li = document.createElement('li');
        li.className = `conv-item ${c.id === currentConvId ? 'active' : ''}`;
        
        // Strictly injecting the three vertical dots (⋮), no gear icon
        li.innerHTML = `
            <span class="conv-title" onclick="loadConversation(${c.id}, '${c.title.replace(/'/g, "\\'")}')">
                ${c.title || `Chat #${c.id}`}
            </span>
            <button class="dots-btn" onclick="toggleMenu(event, ${c.id})">⋮</button>
            <div id="menu-${c.id}" class="context-menu">
                <div onclick="renameChat(${c.id}, '${c.title.replace(/'/g, "\\'")}')">✏️ Rename</div>
                <div class="danger" onclick="deleteChat(${c.id})">🗑️ Delete</div>
            </div>
        `;
        list.appendChild(li);
    });
}

function toggleMenu(event, id) {
    event.stopPropagation();
    document.querySelectorAll('.context-menu').forEach(m => {
        if (m.id !== `menu-${id}`) m.classList.remove('show');
    });
    document.getElementById(`menu-${id}`).classList.toggle('show');
}

async function renameChat(id, oldTitle) {
    const newTitle = prompt("Enter new title:", oldTitle);
    if (newTitle && newTitle.trim() !== "") {
        await fetch(`/api/conversations/${id}/rename`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle.trim() })
        });
        if (id === currentConvId) document.getElementById('chat-title').innerText = newTitle.trim();
        refreshSidebar();
    }
}

async function deleteChat(id) {
    if (confirm("Are you sure you want to delete this chat completely?")) {
        await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
        
        if (id === currentConvId) {
            document.getElementById('chat-container').innerHTML = '';
            document.getElementById('chat-title').innerText = 'AI Voice Agent';
            currentConvId = null;
        }
        refreshSidebar();
    }
}

async function startNewChat() {
    const res = await fetch('/api/conversations/new', { method: 'POST' });
    const data = await res.json();
    currentConvId = data.conversation_id;
    document.getElementById('chat-title').innerText = data.title;
    document.getElementById('chat-container').innerHTML = '';
    stopAudio();
    await refreshSidebar();
    closeSidebarOnMobile();
}

function appendMessageUI(role, content, audio1 = null, audio2 = null) {
    const container = document.getElementById('chat-container');
    if (!container) return;

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`; 

    if (role === 'assistant') {
        const textDiv = document.createElement('div');
        textDiv.className = 'msg-content';
        textDiv.innerText = content;
        msgDiv.appendChild(textDiv);

        if (audio1) {
            const audioBtn = document.createElement('button');
            
            if (audio1 === 'pending' || audio1.includes('processing')) {
                audioBtn.className = 'audio-control-btn loading';
                audioBtn.innerHTML = '⏳ Generating Audio...';
                audioBtn.disabled = true;
            } else if (!audio1.includes('fake-url')) {
                audioBtn.className = 'audio-control-btn';
                audioBtn.innerHTML = '▶ Play Voice';
                audioBtn.onclick = () => handleAudioToggle(audioBtn, audio1, audio2);
            }
            msgDiv.appendChild(audioBtn);
        }
    } else {
        msgDiv.innerText = content;
    }

    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;
}

async function loadConversation(id, title) {
    currentConvId = id;
    document.getElementById('chat-title').innerText = title;
    const container = document.getElementById('chat-container');
    container.innerHTML = '';
    stopAudio();
    closeSidebarOnMobile();

    const res = await fetch(`/api/conversations/${id}/messages`);
    const messages = await res.json();
    
    messages.forEach(msg => appendMessageUI(msg.role, msg.content, msg.audio_url, msg.audio_url_2));
    scrollToBottom();
    await refreshSidebar();
    checkAndPollAudio();
}

function handleAudioToggle(btn, audio1Url, audio2Url) {
    if (currentAudioButton === btn && currentAudio) {
        if (!currentAudio.paused) {
            currentAudio.pause();
            btn.innerHTML = '▶ Play Voice';
        } else {
            currentAudio.play();
            btn.innerHTML = '⏸ Pause Voice';
        }
        return;
    }

    stopAudio();

    btn.innerHTML = '⏸ Pause Voice';
    currentAudioButton = btn;
    currentAudio = new Audio(audio1Url);
    
    currentAudio.play().catch(err => console.error("Audio 1 Error:", err));

    currentAudio.addEventListener('ended', () => {
        if (audio2Url && !audio2Url.includes('fake-url')) {
            currentAudio = new Audio(audio2Url);
            currentAudio.play().catch(err => console.error("Audio 2 Error:", err));
            
            currentAudio.addEventListener('ended', () => stopAudio());
        } else {
            stopAudio();
        }
    });
}

function stopAudio() {
    if (currentAudio) {
        currentAudio.pause();
        currentAudio = null;
    }
    if (currentAudioButton) {
        currentAudioButton.innerHTML = '▶ Play Voice';
        currentAudioButton = null;
    }
}

function showTypingIndicator() {
    const container = document.getElementById('chat-container');
    const div = document.createElement('div');
    div.className = 'typing-indicator';
    div.id = 'typing-bubble';
    div.innerHTML = '<span></span><span></span><span></span>';
    container.appendChild(div);
    scrollToBottom();
}

function removeTypingIndicator() {
    const bubble = document.getElementById('typing-bubble');
    if (bubble) bubble.remove();
}

async function sendMessage() {
    if (isProcessing) return;

    const input = document.getElementById('user-input');
    const text = input.value.trim();
    const ttsEnabled = document.getElementById('tts-toggle').checked;
    const sendBtn = document.querySelector('.send-btn');

    if (!text) return;
    if (!currentConvId) await startNewChat();

    isProcessing = true;
    input.disabled = true;
    sendBtn.disabled = true;

    const container = document.getElementById('chat-container');
    const userDiv = document.createElement('div');
    userDiv.className = 'message user';
    userDiv.innerText = text;
    container.appendChild(userDiv);
    input.value = '';
    
    showTypingIndicator();

    let isUnlocked = false;
    const unlockUI = () => {
        if (!isUnlocked) {
            isProcessing = false;
            input.disabled = false;
            sendBtn.disabled = false;
            input.focus();
            isUnlocked = true;
        }
    };

    try {
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ 
                conversation_id: currentConvId, 
                user_input: text,
                tts_enabled: ttsEnabled
            })
        });

        if (res.status === 401 || res.status === 403) return promptLogout();

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        
        let assistantMsgDiv = null;
        let textDiv = null;
        let audioBtn = null;
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            
            let boundary = buffer.indexOf('\n\n');
            while (boundary !== -1) {
                const chunkStr = buffer.slice(0, boundary).trim();
                buffer = buffer.slice(boundary + 2); 
                boundary = buffer.indexOf('\n\n'); 
                
                if (chunkStr.startsWith('data: ')) {
                    const data = JSON.parse(chunkStr.substring(6));
                    
                    if (data.type === 'token') {
                        if (!assistantMsgDiv) {
                            removeTypingIndicator();
                            
                            assistantMsgDiv = document.createElement('div');
                            assistantMsgDiv.className = 'message assistant';
                            
                            textDiv = document.createElement('div');
                            textDiv.className = 'msg-content';
                            assistantMsgDiv.appendChild(textDiv);

                            if (ttsEnabled) {
                                audioBtn = document.createElement('button');
                                audioBtn.className = 'audio-control-btn loading';
                                audioBtn.innerHTML = '⏳ Generating Audio...';
                                audioBtn.disabled = true;
                                assistantMsgDiv.appendChild(audioBtn);
                            }
                            
                            container.appendChild(assistantMsgDiv);
                        }
                        
                        textDiv.innerText += data.content;
                        scrollToBottom();
                    } 
                    else if (data.type === 'text_complete') {
                        unlockUI();
                    }
                    else if (data.type === 'audio' && audioBtn) {
                        audioBtn.className = 'audio-control-btn';
                        audioBtn.innerHTML = '▶ Play Voice';
                        audioBtn.disabled = false;
                        audioBtn.onclick = () => handleAudioToggle(audioBtn, data.audio_url, data.audio_url_2);
                    }
                    else if (data.type === 'audio_error' && audioBtn) {
                        audioBtn.className = 'audio-control-btn';
                        audioBtn.style.background = '#64748b';
                        audioBtn.innerHTML = '⚠️ Audio Failed';
                    }
                    else if (data.type === 'done') {
                        await refreshSidebar();
                    }
                }
            }
        }
    } catch (error) {
        removeTypingIndicator();
        console.error("Streaming chat error:", error);
    } finally {
        unlockUI();
    }
}

function scrollToBottom() {
    const container = document.getElementById('chat-container');
    container.scrollTop = container.scrollHeight;
}

// --- Theme Logic ---
function toggleTheme() {
    document.body.classList.toggle('dark-theme');
    const isDark = document.body.classList.contains('dark-theme');
    localStorage.setItem('theme', isDark ? 'dark' : 'light');
}

// --- Logout Modal Logic ---
function promptLogout() {
    document.getElementById('logout-modal').style.display = 'flex';
}

function closeLogoutModal() {
    document.getElementById('logout-modal').style.display = 'none';
}

async function confirmLogout() {
    await fetch('/api/auth/logout', { method: 'POST' });
    location.reload();
}