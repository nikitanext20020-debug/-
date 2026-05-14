const { app, BrowserWindow } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let mainWindow;
let backendProcess;

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1400,
        height: 900,
        backgroundColor: '#0a0a0f',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        },
        icon: path.join(__dirname, '../assets/icon.png'),
        autoHideMenuBar: true,
        title: 'NEURO.CORE - Telegram Bot Dashboard'
    });

    // Ждём запуска бэкенда
    setTimeout(() => {
        mainWindow.loadURL('http://localhost:5173');
    }, 3000);

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

function startBackend() {
    const pythonPath = process.platform === 'win32' ? 'python' : 'python3';
    const scriptPath = path.join(__dirname, '../dashboard/backend/main.py');

    backendProcess = spawn(pythonPath, [scriptPath], {
        cwd: path.join(__dirname, '..'),
        env: process.env
    });

    backendProcess.stdout.on('data', (data) => {
        console.log(`Backend: ${data}`);
    });

    backendProcess.stderr.on('data', (data) => {
        console.error(`Backend Error: ${data}`);
    });

    backendProcess.on('close', (code) => {
        console.log(`Backend process exited with code ${code}`);
    });
}

function startFrontend() {
    const npmPath = process.platform === 'win32' ? 'npm.cmd' : 'npm';
    const frontendPath = path.join(__dirname, '../dashboard/frontend');

    const frontendProcess = spawn(npmPath, ['run', 'dev'], {
        cwd: frontendPath,
        env: process.env,
        shell: true
    });

    frontendProcess.stdout.on('data', (data) => {
        console.log(`Frontend: ${data}`);
    });

    frontendProcess.stderr.on('data', (data) => {
        console.error(`Frontend: ${data}`);
    });
}

app.whenReady().then(() => {
    console.log('Starting NEURO.CORE application...');

    // Запускаем бэкенд
    startBackend();

    // Запускаем фронтенд
    startFrontend();

    // Создаём окно
    createWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        if (backendProcess) {
            backendProcess.kill();
        }
        app.quit();
    }
});

app.on('before-quit', () => {
    if (backendProcess) {
        backendProcess.kill();
    }
});
