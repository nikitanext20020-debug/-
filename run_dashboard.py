import subprocess
import sys
import os
import time
import webbrowser

def run_dashboard():
    print("🚀 Запуск Dashboard для Нейрокомментинга...")
    
    # 1. Запуск бэкенда
    backend_p = subprocess.Popen(
        [sys.executable, "dashboard/backend/main.py"],
        cwd=os.getcwd()
    )
    
    print("⏳ Ожидание запуска бэкенда...")
    time.sleep(2)
    
    # 2. Запуск фронтенда
    print("🚀 Запуск фронтенда...")
    frontend_p = subprocess.Popen(
        "npm run dev", 
        cwd="dashboard/frontend",
        shell=True
    )
    
    # Даем время на инициализацию Vite
    time.sleep(3)
    
    frontend_url = "http://localhost:5173" 
    
    print(f"\n✅ Бэкенд запущен на http://localhost:8000")
    print(f"✅ Фронтенд запускается на {frontend_url}")
    print(f"👉 Откройте {frontend_url} в браузере (должно открыться автоматически)")
    
    # Попробуем открыть браузер автоматически
    webbrowser.open(frontend_url)
    
    try:
        backend_p.wait()
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")
        backend_p.terminate()

if __name__ == "__main__":
    run_dashboard()
