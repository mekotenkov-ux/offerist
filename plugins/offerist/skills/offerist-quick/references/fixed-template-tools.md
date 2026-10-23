# Сборка фиксированного КП

Перед `--client-ready` заполни и проверь `research_handoff` по [контракту исходников и достаточности](../../offerist/references/evidence-handoff-and-depth.md). Авторские проверки ведут к оригиналам, `analysis_links` — к полным безопасным выгрузкам; внутренняя записка остаётся внутренней. Статус неподготовленного прогноза сохраняется без выдуманных рядов.

Команды выполняются из корня установленного плагина. `RUN` ниже означает отдельную рабочую папку клиента вне кэша плагина. Python нужен для подготовки данных и запросов, Node.js с Playwright и установленный Chromium — для изображений и PDF. Пути к доступным средам возьми через `load_workspace_dependencies`; не устанавливай новый браузер при наличии рабочего.

## Данные и изображения

```text
python scripts/proposal_contract.py init --output RUN/proposal.json
python scripts/proposal_contract.py validate RUN/proposal.json --client-ready
python scripts/proposal_preview.py --input RUN/proposal.json --output RUN/preview/catalog.html
node scripts/render_proposal_gallery.mjs --input RUN/preview/catalog.html --output RUN/preview --modules-root NODE_MODULES
```

После `init` агент заполняет `proposal.json` из анализов по [контракту v2](fixed-template.md) и [правилам авторского наполнения](authoring-evidence.md). Пустой файл намеренно не проходит приёмку. Заполни `intro`, `analysis_links`, `takeaway`, фиксированные таблицы и `details`, а при основном графике — `visual_explanation`. У аналитических разделов обязателен связный абзац и реальное доказательство с интерпретацией. `source`/`source_url` остаются внутренними метаданными; прежний `action` не выводится хвостом. Для понимания схемы доступен `examples/fixed-template-demo.json`; его синтетические факты нельзя принимать с `--client-ready` или переносить клиенту.

В `preview/pages` находятся PNG и SVG каждого раздела, в `preview/visuals` — отдельные графики и схемы. `catalog.pdf` служит проверкой локальной вёрстки. Все таблицы и подписи строятся из данных, нумерация берётся из шаблона. Не вставляй PNG целой страницы в Google Docs вместо редактируемых элементов.

## Новый Google Документ

Создай пустой native Google Doc подключённым инструментом Google Drive. Получи его актуальный `get_document` с `documentId`, `revisionId`, `documentStyle`, `namedStyles`, содержимым и свойствами вкладок. Сохрани ответ в `RUN/native/00-blank.json`. Компилятор принимает как сам объект документа, так и обёртку `structuredContent`. Если вкладок несколько, выбери нужную через `--tab-id`.

Стадии выполняются последовательно: `skeleton` → `tables` → `fill` → `images` → `style`. После каждой стадии прочитай документ заново и передай свежий readback следующей стадии.

```text
python scripts/proposal_docs.py skeleton --data RUN/proposal.json --readback RUN/native/00-blank.json --out RUN/native/01-plan.json
python scripts/proposal_docs.py tables --data RUN/proposal.json --readback RUN/native/01-readback.json --out RUN/native/02-plan.json
python scripts/proposal_docs.py fill --data RUN/proposal.json --readback RUN/native/02-readback.json --out RUN/native/03-plan.json
python scripts/proposal_docs.py images --data RUN/proposal.json --readback RUN/native/03-readback.json --visual-dir RUN/preview/visuals --out RUN/native/04-plan.json
python scripts/proposal_docs.py style --data RUN/proposal.json --readback RUN/native/04-readback.json --out RUN/native/05-plan.json
```

Каждый `plan.json` содержит `document_id` и `batches`. Исполни `requests` каждого пакета через подключённый `batch_update_document`, сохранив `write_control`. Для второго и последующих пакетов используй ревизию из ответа предыдущей записи. Если она не пришла, сначала прочитай документ. При неопределённом результате записи сверяй фактическое содержимое; слепой повтор вставок дублирует текст и ломает индексы.

Если пакет содержит `image_uris`, передай его отдельным одноимённым параметром инструмента вместе с `requests`. Это путь к локальному изображению для загрузки управляемым коннектором. Не создавай публичный файловый сервер и не подставляй base64 в текст документа. Стадия `images` не создаёт доказательств: основной `visual` использует подготовленный файл, а изображения в `details` — реальные локальные файлы по указанному `path`. Сохрани подпись, интерпретацию и ссылки рядом со вставкой.

Компилятор рассчитывает индексы UTF-16 по реальному документу. Не вычисляй смещения вручную и не отправляй план, созданный для другой ревизии. Он предназначен для сборки нового документа. Узкую правку существующего КП выполняй адресно по свежим индексам, сохранив его ID и остальное содержимое.

## Word по запросу

Для DOCX используй тот же JSON v2 и подготовленные изображения:

```text
python skills/offerist-quick/scripts/build_docx.py --input RUN/proposal.json --output RUN/proposal.docx --visual-dir RUN/preview/visuals
```

Этот путь использует общее представление документа v2 без промежуточного Markdown: скелет, авторские подробности, ссылки, оценки и изображения берутся из тех же данных. Прежний маршрут Markdown сохраняется только для явного запроса старого оформления. Экспорт DOCX требует просмотра его собственного рендера; успешная запись файла не подтверждает вёрстку.

## Приёмка

```text
python scripts/proposal_docs.py verify --data RUN/proposal.json --readback RUN/native/05-readback.json --out RUN/native/verification.json
```

Проверка сверяет ожидаемые заголовки, текст и таблицы, карточки оценок, размещение изображений по разделам и основные параметры оформления. Это проверка структуры и части стилей. Её `ok` не означает, что просмотрен PDF, подтверждены данные анализа или раздел понятен без открытия приложения. Подводка со ссылками должна быть сверху, текст — обычным; зелёный вывод, таблица возможностей, оценки и доказательства идут в установленном порядке. Отдельно прочитай авторское объяснение от «мы» и расшифровку каждого графика.

Экспортируй ту же финальную версию через Google Drive в PDF, отрендери и просмотри страницы. Проверь переносы таблиц, крупные оценки, подписи графиков, ссылки, дорожную карту и пустые денежные поля. После исправлений обнови readback, повтори затронутую проверку и экспорт. Сохрани проверенную ревизию и найденные дефекты; не назначай «просмотрено» программно всем страницам.

Проверяющий отдельно получает ТЗ владельца, референсы и исходные анализы. Визуальный каталог и учебный Google Документ — эталоны оформления; ни один из них не является источником клиентских фактов.

Технические источники: [операции с таблицами Google Docs](https://developers.google.com/workspace/docs/api/how-tos/tables), [стили строк и защита от разрыва](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents#TableRowStyle), [закрепление шапки таблицы](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#PinTableHeaderRowsRequest). Для закрепления шапки служит `pinTableHeaderRows`; попытка записать `tableHeader` через `updateTableRowStyle` отклоняется API. Само наличие параметров в запросе не доказывает корректный перенос: проверяется реальный экспорт.
