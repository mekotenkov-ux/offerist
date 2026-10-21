# Офферист — SEO/GEO коммерческие предложения

**Текущая публичная версия: 1.2.0-evidence-public.1.** 25 навыков для анализа, обоснования работ и сборки Google Docs / Word. [Что изменилось](docs/release-1.2.0.md).

## Установка и обновление

```bash
codex plugin marketplace add mekotenkov-ux/offerist --ref main
codex plugin add offerist@seomeat-offerist
codex plugin list
```

Если marketplace уже добавлен:

```bash
codex plugin marketplace upgrade seomeat-offerist
codex plugin add offerist@seomeat-offerist
codex plugin list
```

Начните новый чат после обновления. [Полная инструкция и перенос в другую среду](INSTALL.md).

## Работа

- `$offerist-quick` — готовое КП по данным проекта и шаблону v2.
- `$offerist-assistant` — совместная разработка и адресные правки.
- `$offerist-brief` — брифовка и переписка.
- `$offerist-setup` — условия агентства и свои эталоны.
- [Все 25 навыков](docs/skills.md), [описание плагина](plugins/offerist/README.md).

Каждый аналитический блок объясняет, что мы проверили, что увидели и какие работы предлагаем. Ссылки на анализы стоят сверху; вывод выделен зелёным. Внутри — оценки, таблицы, реальные примеры, скриншоты и расшифровки графиков. Основные разделы имеют фиксированную римскую нумерацию. Дорожная карта показывает работы по строкам и месяцы по столбцам, объёмы либо галочки в ячейках.

[Шаблон](plugins/offerist/skills/offerist-quick/references/fixed-template.md) и [команды сборки](plugins/offerist/skills/offerist-quick/references/fixed-template-tools.md) используют общий proposal.json. [Правила авторского объяснения](plugins/offerist/skills/offerist-quick/references/authoring-evidence.md) дополняют каркас. Встроенная схема не заменяет анализ и чтение результата.

## Материалы и подключения

Публичные примеры вымышлены. Частные КП, исходные скриншоты клиентов, ключи и профили в репозиторий не входят. Агентство добавляет собственные эталоны через [reference_pack](plugins/offerist/skills/offerist-quick/references/reference-basis.md). Установка не переносит авторизацию и не добавляет отсутствующие инструменты среды. [Подключения](plugins/offerist/docs/connectors.md).

Прежние [Word/PDF примеры](examples/demo/README.md) сохранены как учебные материалы старой формы; актуальное оформление задаёт v2. Примеры [брифовки](examples/briefing/briefing.md) и [совместной работы](examples/freeform/dialogue.md) остаются доступными.

## Статус проверки

Новые тесты и пробные генерации при публикации не запускались по указанию владельца. Выполнена статическая сверка публичного состава и ссылок. Публикация не подтверждает доступ к вашим API или качество будущего КП. [Условия распространения](LICENSE.md).
