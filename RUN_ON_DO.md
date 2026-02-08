# Запуск бота на DigitalOcean

## После развертывания приложения:

1. **Подключитесь к консоли на DigitalOcean**
   - Перейдите в Console на сайте DigitalOcean
   - Откройте приложение

2. **Установите переменные окружения**
   - Перейдите в Settings → Environment
   - Добавьте переменные:
     - `TELEGRAM_BOT_TOKEN` = ваш токен от @BotFather
     - `EMAIL_FROM` = ваша email адреса
     - `EMAIL_PASSWORD` = пароль от email
   - Нажмите "Save"

3. **Загрузите Firebase ключ (если его нет)**
   - Скачайте `serviceAccountKey.json` из Firebase Console
   - Загрузите его через файловый менеджер DigitalOcean

4. **Переразверните приложение**
   - Нажмите "Redeploy" для применения переменных окружения

5. **Запустите бота**
   - Перейдите на вкладку "Components"
   - Найдите процесс "web"
   - Нажмите на него и измените команду на:
     ```
     python3 bot_bgpk.py
     ```
   - Сохраните

Или используйте CLI:
```bash
# Подключитесь к вашему приложению
doctl apps create-deployment YOUR_APP_ID --source-digest YOUR_DIGEST

# Или перезагрузите через консоль
```

## Как остановить бота:
- Измените команду web процесса на: `python3 -c "import sys; sys.exit(0)"`
- Переразверните

## Для постоянной работы:
Рекомендуется использовать отдельный Worker процесс вместо Web.
