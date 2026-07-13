# Ф23: Alt-Tab режим — исследование KDE/Wayland

Дата: 2026-07-13. Вопрос: может ли обычное непривилегированное приложение
PySide6 над Wayland надёжно дождаться именно отпускания модификатора хоткея
(`Ctrl` в `Ctrl+``) и после этого вставить выбранный клип?

## Вывод

**Нет, не для требуемой семантики.** На KDE Plasma приложение может честно
получить глобальные press/repeat/release *действия* от KGlobalAccel по D-Bus,
но release не означает «отпущен Ctrl». В текущей реализации KGlobalAccelD
release отправляется при первом отпускании любого ключа активного shortcut.
При естественном `Ctrl` → `` ` `` → отпустить `` ` `` → снова `` ` `` →
отпустить Ctrl, первый `globalShortcutReleased` придёт уже на отпускание
`` ` ``. После него daemon очищает active action, поэтому клиент не получает
следующее отдельное уведомление об отпускании Ctrl.

Следовательно, для Ф23 нельзя реализовать заявленное «отпустить модификатор —
вставить» через существующий KGlobalAccel backend и выдавать это за надёжный
Wayland-функционал. Правильное закрытие Alt-Tab под текущим критерием —
**unavailable / невозможно без platform-specific hacks**. Persistent-режим
при этом независим и может быть реализован нормально.

## Что именно предоставляет KDE

1. [Официальный API KGlobalAccel](https://api.kde.org/kglobalaccel.html)
   описывает global shortcut как независимый от фокуса окна и публикует
   `globalShortcutActiveChanged(action, active)`: действие становится active
   при нажатии его клавиш и inactive при их отпускании.
2. Официальная D-Bus спецификация, установленная вместе с KF6 на этой машине:
   `/usr/share/dbus-1/interfaces/kf6_org.kde.kglobalaccel.Component.xml`.
   У component есть ровно три подходящих сигнала с идентификатором *действия*,
   а не физического ключа: `globalShortcutPressed`,
   `globalShortcutRepeated`, `globalShortcutReleased`.
3. Исходник [KGlobalAccelD `Component`](https://github.com/KDE/kglobalacceld/blob/master/src/component.cpp)
   лишь преобразует состояние shortcut в эти три сигнала. Его
   [реестр shortcut](https://github.com/KDE/kglobalacceld/blob/master/src/globalshortcutsregistry.cpp)
   при любом `ShortcutKeyState::Released`, если есть `m_lastShortcut`, посылает
   `Released` и очищает `m_lastShortcut` (функция `keyEvent`, строки около
   389–438). Это не API наблюдения за конкретным modifier.
4. Текущий Keeps подключается только к `globalShortcutPressed` в
   `src/keeps/hotkey/kglobalaccel.py`; технически можно подключить ещё
   `Repeated`/`Released`, но это не устраняет указанную семантическую проблему.

`Repeated` тоже не является заменой «повторного нажатия `` ` `` при удержании
Ctrl»: это событие autorepeat, а не самостоятельный поток сырых клавиш.

## Почему Qt/Wayland не даёт честного обхода

[Стандарт Wayland](https://wayland.freedesktop.org/docs/html/apa.html)
задаёт keyboard focus: `wl_keyboard` сообщает enter/leave для поверхности с
keyboard focus, а key/modifier events относятся к этому фокусу. Обычный клиент
не получает глобальный поток физической клавиатуры других приложений. Поэтому
`QKeyEvent::KeyRelease` — не решение до тех пор, пока окно Keeps не стало
фокусным; это гонка с показом/активацией popup и не глобальная гарантия.
[Qt QKeyEvent](https://doc.qt.io/qt-6/qkeyevent.html) также прямо описывает
доставку press/release виджету с keyboard input focus.

`QGuiApplication.queryKeyboardModifiers()` умеет запросить текущее состояние
модификаторов, но не даёт уведомление об изменении. Polling после release
KGlobalAccel был бы тайминговой эвристикой и на Wayland всё равно опирался бы
на состояние, доставленное фокусному клиенту; это не соответствует критерию
«надёжно». `xkbcommon` может интерпретировать уже полученное состояние, но не
даёт приложению права читать глобальную клавиатуру.

Прямое чтение `/dev/input/event*` требует прав/ACL и ломает модель Wayland.
`ydotool` не помогает: его [первичный README](https://github.com/ReimuNotMoe/ydotool)
описывает `ydotoold` как persistent `uinput` **виртуальное устройство для
эмуляции ввода**; это write-path, а не разрешённый API наблюдения за реальными
клавишами. Использовать его для инъекции или прослушивать evdev было бы именно
неподдерживаемым хаком, а не решением Ф23.

## Portal не меняет результат

[XDG GlobalShortcuts portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html)
может в будущем быть cross-DE backend после user-consent. Однако он также
доставляет lifecycle зарегистрированного shortcut (`Activated` / `Deactivated`)
по `shortcut_id`, а не отдельный release конкретного Ctrl. Он не реализует
нужный протокол Alt-Tab сам по себе.

## Рекомендация для реализации Ф23

- Реализовать только persistent toggle: pin в title bar, не скрывать popup при
  потере фокуса или вставке, но `Esc` всегда скрывает; не сохранять тумблер
  между сессиями.
- **Не добавлять** Alt-Tab setting, который выглядит рабочим. Зафиксировать в
  PLAN и `MANUAL_TESTING.md`, что exact Alt-Tab mode unavailable на Wayland без
  hacks; оставить default off.
- Если когда-либо понадобится изменённый UX, проектировать его как отдельную
  функцию с явным подтверждением (`Enter`/второй hotkey), а не как «вставка на
  release Ctrl». Это уже не критерий текущей Ф23.

