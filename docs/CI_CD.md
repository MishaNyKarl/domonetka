# CI/CD

Репозиторий: https://github.com/MishaNyKarl/domonetka. Workflow: `.github/workflows/ci-cd.yml`.

Каждый push в `main`, pull request и ручной запуск проверяет Python, изолированную
SQLite, Binom, SSO и таблицу отчётов в Chromium. SSO SDK закреплён на commit
`08d057fd214d58d4210032916025566f5e1550c1` (0.1.2), соответствующий серверному окружению.
Для PR из внешних forks закрытый SDK недоступен: SSO-тесты пропускаются; такой PR
никогда не деплоится. Push в `main` обязательно проходит полные SSO-проверки.

После успешных проверок push в `main` автоматически обновляет production, если
repository variable `DEPLOY_ENABLED=true`. Ручной запуск из `main` делает то же.
Релизы выполняются последовательно; запущенный деплой не отменяется новым push.

## Первое подключение сервера

Ключи созданы локально в игнорируемом каталоге `.ci-private/`. Один ключ позволяет
CI только читать закрытый SDK; другой — запускать ограниченный forced command
на сервере. Ключ деплоя не даёт интерактивный root shell или SSH forwarding.

Из корня проекта в PowerShell выполнить:

```powershell
.\.venv\Scripts\python.exe deploy\bootstrap_ci.py
```

Ввести SSH-пароль в терминале. Скрипт установит root-owned receiver в
`/usr/local/lib/domonetka/receive_release.py`, добавит ограниченный ключ в
`/root/.ssh/authorized_keys`, проверит вход ключом, включит `DEPLOY_ENABLED`
через `gh` и запустит первый workflow. Пароль не сохраняется и не отправляется GitHub.
Нужны авторизованный `gh`, известный SSH host key и `paramiko` в локальном venv.

GitHub repository secret `SSO_READ_KEY`: отдельный read-only deploy key репозитория
`MishaNyKarl/opsvault-sso`. Environment `production`: секреты `DEPLOY_KEY` и
`DEPLOY_KNOWN_HOSTS`. Repository variables: `DEPLOY_HOST=176.100.37.104`,
`DEPLOY_ENABLED=false` до успешного bootstrap. Host key проверяется строго.

## Что обновляется

Архив содержит только пять runtime-модулей Python, `public/` и `public/version.txt`
с SHA коммита. `.env`, SQLite, загруженные изображения, закрытые SSO-материалы,
локальные ключи и настройки systemd не попадают в архив или Git.
Существующее серверное окружение SSO и параметры сервиса сохраняются.

Receiver проверяет пути архива и Python-код, сохраняет прежние файлы, останавливает
сервис, резервирует SQLite и Binom JSON, устанавливает релиз и запускает сервис.
Если локальная проверка health, версии, favicon или админки не проходит,
возвращаются прежние код и база, затем запускается предыдущая версия.
Резервные копии: `/root/domonetka-ci-backups/<UTC>-<SHA>/` (root-only).
Автоматического удаления резервных копий нет. Во время обновления возможна краткая
недоступность. После запуска CI проверяет версию и favicon через публичный HTTPS.
Ошибка внешней HTTPS-проверки отмечает workflow как failed; она не вызывает
повторный откат уже здорового локального сервиса.

## Отключение

```powershell
gh variable set DEPLOY_ENABLED --body false --repo MishaNyKarl/domonetka
```

Проверки продолжат работать, автоматическая загрузка на сервер остановится.
Для обновления root-owned receiver повторно запустить bootstrap после review.
