# Контракт отчёта Binom

GET /api/binom/report?from=YYYY-MM-DD&to=YYYY-MM-DD&stream_id=ID.
Оба дня UTC; без дат возвращаются точные снимки за всё время. Пользователь видит
только текущие привязки своих потоков; администратор — всех.

Ответ: {rows, daily, columns, bindings, sync, period:{from,to,timezone:'UTC'}}.
rows — итоги периода; daily — дневные снимки за выбранный период, без дат — все доступные дни.
Каждая строка: {offer_id,stream_id,campaign_id,user_id,scope,variant_id,day,
metrics:{динамический ключ:number|null},statuses:[{one,two,leads}],updated,
approved:null|number,pending:null|number,rejected:null|number,unknown:null|number,
approval_rate:null|number,precision_warnings:[]}.

scope='campaign': вся кампания Binom. scope='attributed': только совпавшие
dm_source/flow/offer. scope='variant': также dm_variant, включая прежние варианты.
Итоги campaign/attributed/variant нельзя складывать между собой.
day='all' для точных общих снимков, 'period' для агрегатов периода; daily содержит даты.
Отсутствие строки = импорт не выполнен/недоступен; успешный ноль — строка с нулями.

columns: [{user_id,key,name,decimals,format,custom}]. Каталог динамический; скрытые
в Binom поля могут не возвращаться API. Не заменять отсутствующие показатели нулями.
Основные keys: clicks,unique_clicks,lander_views,lander_clicks,lander_ctr,leads,cr,
bots,epc,cpc,revenue,cost,profit,roi,event_1..event_30 и дополнительные доступные ключи.

За всё время и за выбранный период получаются точные отчёты Binom, включая
уникальные, коэффициенты и custom-показатели. Приложение не суммирует дневные
коэффициенты. Неподготовленный период ставится в фоновую очередь; pending_jobs
сообщает queued/running. Последний успешный снимок сохраняется при ошибке.
Статусы сохраняются парами one/two. Категории задаются списками выбранной у
баннера партнёрки, принадлежащей владельцу потока. Без списков категории null.
Строка также содержит affiliate_id и affiliate_name.

bindings: [{offer_id,user_id,campaign_id,stream_id,status,error,last_attempt,
last_success,attribution_ready,warning}]. status=never|running|success|error.
Ошибка не удаляет последний хороший снимок. После смены кампании/владельца/адреса/
токенов старые снимки не показываются как текущие.
sync: [{user_id,next_due,last_started,last_finished,last_status,error,job_id}].

POST /api/binom/sync {user_id?:ID} возвращает 202 {job_id,status}; повторный запуск
того же пользователя во время работы возвращает существующее задание.
Логи доступны /api/binom/logs?user_id=ID. UI не должен ждать HTTP до конца импорта.

GET/POST /api/report/preferences — персональные настройки JSON {preferences:{...}}.
Граница сохранённого JSON — 64 КБ, принадлежность берётся из сессии, не из user_id.
Этот endpoint предназначен для модуля таблицы. Реализацию его выполняет основной чат.

GET /api/admin также возвращает variant_daily:
[{stream_id,offer_id,variant_id,day,impressions,clicks}], с ограничением по
владельцу и выбранным датам. Эти локальные данные соединяются с daily строго
по offer_id/variant_id/day; периодные итоги не используются как дневные.
При импорте выбранного периода обновляются точные итоги и его дневные снимки.
