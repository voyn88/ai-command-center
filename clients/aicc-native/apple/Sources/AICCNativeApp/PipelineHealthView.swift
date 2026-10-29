import SwiftUI
import AICCNativeCore

struct PipelineHealthView: View {
    let snapshot: Snapshot
    let connection: AICCAppModel.ConnectionState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Только просмотр").font(.caption)
                Text(connection.title).foregroundStyle(.secondary)
                Text("Снимок: \(snapshot.generatedAt.formatted(date: .abbreviated, time: .shortened))")
                if snapshot.freshness != .fresh {
                    Label("Данные могут быть неполными или устаревшими", systemImage: "clock.badge.exclamationmark")
                }
                if let health = snapshot.pipelineHealth {
                    healthContent(health)
                } else {
                    Label("Состояние конвейера пока неизвестно", systemImage: "questionmark.circle")
                    Text("Сервер ещё не передал данные конвейера.").foregroundStyle(.secondary)
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle("Конвейер")
    }

    @ViewBuilder private func healthContent(_ health: PipelineHealth) -> some View {
        let model = PipelineHealthViewModel(health: health)
        Label(statusText(model.state), systemImage: statusIcon(model.state))
            .font(.title2.weight(.semibold)).foregroundStyle(statusColor(model.state))
        if let observed = PipelineHealthViewModel.date(health.observedAt) {
            Text("Наблюдение: \(observed.formatted(date: .abbreviated, time: .shortened))").font(.caption)
        }
        if model.state == .down {
            CriticalEscalationBanner(escalation: CriticalEscalation(
                id: "pipeline-down", title: "Конвейер остановлен",
                summary: "Нет активных линий исполнения. Требуется проверка исполнителей.",
                occurredAt: snapshot.generatedAt, severity: .critical, actionLabel: ""
            ))
        }
        if (health.executorAuthFailures30m ?? 0) > 0 {
            Label("Агенты разлогинены — перелогиньте Claude/Codex на worker-01", systemImage: "exclamationmark.lock.fill")
                .foregroundStyle(.orange)
        }
        metric("Ошибки входа за 30 минут", health.executorAuthFailures30m)
        GroupBox("Очередь") {
            VStack(alignment: .leading, spacing: 8) {
                metric("Готовы к работе", health.queue?.ready)
                metric("Взяты в работу", health.queue?.claimed)
                metric("Не выполнены после повторов", health.queue?.dead)
                metric("Завершены за последний час", health.queue?.succeededLastHour)
                Text("Старейшая готовая задача: \(age(health.queue?.oldestReadyAgeSeconds))")
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        GroupBox("Активные линии") {
            VStack(alignment: .leading, spacing: 8) {
                metric("Линий", health.activeLanes?.count)
                ForEach(Array((health.activeLanes ?? []).enumerated()), id: \.offset) { _, lane in
                    Text("\(lane.role ?? "Роль неизвестна") · попыток: \(count(lane.attempts)) · сигнал: \(age(lane.heartbeatAgeSeconds))")
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        GroupBox("План работ") {
            VStack(alignment: .leading, spacing: 8) {
                if let backlog = health.backlogByStatus {
                    if backlog.isEmpty { Text("План пуст") }
                    ForEach(backlog.keys.sorted(), id: \.self) { status in
                        metric(backlogLabel(status), backlog[status])
                    }
                } else { Text("Нет данных о плане") }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        GroupBox("Мониторинг") {
            VStack(alignment: .leading, spacing: 8) {
                if let monitor = health.monitor {
                    metric("Открытых замечаний", monitor.openFindings)
                    if monitor.failures?.contains("dead_letter_growth") == true {
                        Text("Растёт число задач, не выполненных после повторов.")
                    }
                    ForEach(monitor.failures ?? [], id: \.self) { Text($0).font(.caption) }
                } else { Text("Данные мониторинга недоступны") }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        Text("Ждут вашего решения").font(.title2)
        if health.ownerEscalations == nil { Text("Список эскалаций недоступен") }
        else if model.escalations.isEmpty { Text("Эскалаций в снимке нет") }
        ForEach(Array(model.escalations.enumerated()), id: \.offset) { _, item in
            CriticalEscalationBanner(escalation: CriticalEscalation(
                id: item.taskID ?? "unknown", title: item.title ?? "Требуется решение",
                summary: "\(item.taskID ?? "ID неизвестен") · приоритет: \(item.priority ?? "неизвестен") · возраст: \(escalationAge(item, health: health))",
                occurredAt: PipelineHealthViewModel.date(item.updatedAt) ?? snapshot.generatedAt,
                severity: .critical, actionLabel: ""
            ))
        }
    }

    private func count(_ value: Int?) -> String { value.map(String.init) ?? "нет данных" }
    private func metric(_ label: String, _ value: Int?) -> some View {
        Text("\(label): \(count(value))").monospacedDigit()
    }
    private func age(_ seconds: Int?) -> String {
        guard let seconds, seconds >= 0 else { return "нет данных" }
        if seconds < 60 { return "\(seconds) сек." }
        if seconds < 3600 { return "\(seconds / 60) мин." }
        return "\(seconds / 3600) ч. \((seconds % 3600) / 60) мин."
    }
    private func escalationAge(_ item: OwnerEscalation, health: PipelineHealth) -> String {
        guard let updated = PipelineHealthViewModel.date(item.updatedAt) else { return "нет данных" }
        let observed = PipelineHealthViewModel.date(health.observedAt) ?? snapshot.generatedAt
        return age(max(0, Int(observed.timeIntervalSince(updated)))) + " на момент снимка"
    }
    private func backlogLabel(_ status: String) -> String {
        ["OPEN": "Открыты", "IN_PROGRESS": "В работе", "DONE": "Завершены",
         "DEFER_TO_USER": "Ждут владельца", "BLOCKED": "Заблокированы",
         "READY_TO_REVIEW": "Готовы к проверке", "UNTRIAGED": "Не разобраны",
         "SPLIT": "Разделены", "NEEDS_REFINEMENT": "Требуют уточнения",
         "DECIDED": "Решение принято"][status] ?? status
    }
    private func statusText(_ state: PipelineHealthState?) -> String {
        switch state {
        case .ok: "Конвейер работает"
        case .attention: "Конвейеру требуется внимание"
        case .down: "Конвейер остановлен"
        case nil: "Состояние конвейера неизвестно"
        }
    }
    private func statusIcon(_ state: PipelineHealthState?) -> String {
        switch state {
        case .ok: "checkmark.circle.fill"
        case .attention: "exclamationmark.triangle.fill"
        case .down: "xmark.octagon.fill"
        case nil: "questionmark.circle"
        }
    }
    private func statusColor(_ state: PipelineHealthState?) -> Color {
        switch state {
        case .ok: .green
        case .attention: .orange
        case .down: .red
        case nil: .secondary
        }
    }
}
