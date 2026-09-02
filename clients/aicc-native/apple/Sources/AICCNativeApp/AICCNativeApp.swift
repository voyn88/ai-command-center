import SwiftUI
import AICCNativeCore

private enum AICCTheme {
    static let plum = Color(red: 0.34, green: 0.25, blue: 0.43)
    static let lilac = Color(red: 0.91, green: 0.89, blue: 0.98)
    static let peach = Color(red: 1.00, green: 0.88, blue: 0.80)
    static let mint = Color(red: 0.88, green: 0.95, blue: 0.91)
    static let forest = Color(red: 0.16, green: 0.42, blue: 0.32)
}

@main
struct AICCNativeApp: App {
    @StateObject private var model = AICCAppModel()

    var body: some Scene {
        WindowGroup { AICCNativeShell(model: model) }
    }
}

extension Snapshot {
    static let preview = Snapshot(
        schemaVersion: "1.0", revision: "preview", generatedAt: .now, freshness: .fresh,
        tasks: [], lanes: [], events: []
    )
}

private enum AppTab: String, CaseIterable, Identifiable {
    case overview, work, dialogues, decisions, more
    var id: Self { self }
    var title: String { switch self { case .overview: "Сегодня"; case .work: "Работа"; case .dialogues: "Диалоги"; case .decisions: "Решения"; case .more: "Ещё" } }
    var icon: String { switch self { case .overview: "sparkles"; case .work: "checklist"; case .dialogues: "bubble.left.and.bubble.right"; case .decisions: "lightbulb"; case .more: "square.grid.2x2" } }
}

struct AICCNativeShell: View {
    @ObservedObject var model: AICCAppModel
    @StateObject private var attention = AttentionMonitor()
    @Environment(\.scenePhase) private var scenePhase
    @State private var tab: AppTab = .overview
    @State private var showPairing = false

    var body: some View {
        Group {
            if attention.requiresSafeMode {
                // Exits only through the explicit resume action below, not
                // through an incidental tap — a reduced-attention owner
                // should not be startled back into the full interface.
                SafeMinimalModeView(needsAttention: model.snapshot.overview.needsAttention) {
                    attention.recordInteraction()
                }
            } else {
                content
                    .simultaneousGesture(TapGesture().onEnded { attention.recordInteraction() })
                    .onChange(of: tab) { _, _ in attention.recordInteraction() }
            }
        }
        .onChange(of: scenePhase) { _, phase in attention.setForeground(phase == .active) }
        .onChange(of: model.connection) { _, newValue in
            if newValue == .unauthorized { showPairing = true }
            if newValue == .live { showPairing = false }
        }
        .task { if !model.hasCredential { showPairing = true } }
        .sheet(isPresented: $showPairing) {
            PairingView { token in await model.pair(token: token) }
        }
    }

    private var content: some View {
        TabView(selection: $tab) {
            OverviewView(snapshot: model.snapshot, connection: model.connection)
                .tabItem { Label(AppTab.overview.title, systemImage: AppTab.overview.icon) }.tag(AppTab.overview)
            WorkView(tasks: model.snapshot.tasks)
                .tabItem { Label(AppTab.work.title, systemImage: AppTab.work.icon) }.tag(AppTab.work)
            DialoguesView(dialogs: model.dialogs)
                .tabItem { Label(AppTab.dialogues.title, systemImage: AppTab.dialogues.icon) }.tag(AppTab.dialogues)
            DecisionsView()
                .tabItem { Label(AppTab.decisions.title, systemImage: AppTab.decisions.icon) }.tag(AppTab.decisions)
            MoreView(events: model.snapshot.events, connection: model.connection)
                .tabItem { Label(AppTab.more.title, systemImage: AppTab.more.icon) }.tag(AppTab.more)
        }
        .tint(AICCTheme.plum)
        .task { await model.refresh() }
    }
}

private struct PairingView: View {
    let onPair: (String) async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var token = ""
    @State private var busy = false

    init(onPair: @escaping (String) async -> Void) { self.onPair = onPair }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 18) {
                Text("Подключение к AICC")
                    .font(.system(.largeTitle, design: .serif, weight: .medium))
                Text("Вставьте токен устройства — его выдаёт ваш сервер AICC один раз при добавлении устройства. Токен хранится только в связке ключей этого устройства.")
                    .foregroundStyle(.secondary)
                SecureField("Токен устройства", text: $token)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                Button {
                    busy = true
                    _Concurrency.Task { await onPair(token); busy = false }
                } label: {
                    if busy { ProgressView() } else { Text("Сохранить и подключиться") }
                }
                .buttonStyle(.borderedProminent)
                .tint(AICCTheme.plum)
                .disabled(token.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                Spacer()
            }
            .padding(24)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Позже") { dismiss() } } }
        }
    }
}

/// The safe minimal layout: one calm status line, no tabs, no lists, no
/// navigation — just what is safe to know right now and a deliberate way
/// back to the full interface.
private struct SafeMinimalModeView: View {
    let needsAttention: Int
    let onResume: () -> Void

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: needsAttention == 0 ? "checkmark.circle" : "eye.circle")
                .font(.system(size: 54))
                .foregroundStyle(needsAttention == 0 ? AICCTheme.forest : .orange)
            Text(needsAttention == 0 ? "Всё спокойно." : "Есть один вопрос на будущее.")
                .font(.system(.title, design: .serif, weight: .medium))
                .multilineTextAlignment(.center)
            Text("Интерфейс упростился — вы давно не взаимодействовали с ним. Ничего срочного нет.")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Button("Вернуться к обзору", action: onResume)
                .buttonStyle(.borderedProminent)
                .tint(AICCTheme.plum)
                .controlSize(.large)
            Spacer()
            Spacer()
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(AICCTheme.lilac.opacity(0.4))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            needsAttention == 0
                ? "Безопасный минимальный режим. Всё спокойно."
                : "Безопасный минимальный режим. Есть один вопрос на будущее."
        )
    }
}

private struct OverviewView: View {
    let snapshot: Snapshot
    let connection: AICCAppModel.ConnectionState

    // Attention first, then the busiest — a calm reader sees what matters.
    private var topProjects: [Project] {
        Array(
            snapshot.projects
                .sorted { ($0.needsAttention, $0.activeTasks) > ($1.needsAttention, $1.activeTasks) }
                .prefix(6)
        )
    }

    private func projectDetail(_ project: Project) -> String {
        if project.needsAttention > 0 {
            return "Требует внимания: \(project.needsAttention) · в работе: \(project.activeTasks)"
        }
        if project.activeTasks > 0 {
            return "В работе: \(project.activeTasks), всё идёт по плану"
        }
        return "Сейчас ничего не требует участия"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("ДОБРОЕ УТРО").font(.caption2.weight(.bold)).tracking(1.3).foregroundStyle(AICCTheme.plum)
                    Text("Всё идёт\nсвоим ходом.").font(.system(size: 46, weight: .medium, design: .serif)).tracking(-1.5)
                    Text("Я собрала главное и оставила вам только то, что действительно заслуживает внимания.")
                        .font(.title3).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    CalmStatus(freshness: snapshot.freshness, needsAttention: snapshot.overview.needsAttention, connection: connection)
                    ProgressCard(goal: snapshot.goal)
                    Text("Ваши проекты").font(.title2.weight(.semibold))
                    if snapshot.projects.isEmpty {
                        ProjectRow(name: "Ваш портфель", detail: "Проекты появятся вместе с данными сервера", color: .gray)
                    }
                    ForEach(topProjects) { project in
                        ProjectRow(
                            name: project.name,
                            detail: projectDetail(project),
                            color: project.needsAttention > 0 ? .orange : (project.activeTasks > 0 ? AICCTheme.forest : .gray)
                        )
                    }
                }.padding()
            }
            .navigationTitle("AICC")
            .toolbar { ToolbarItem(placement: .primaryAction) { Button("Спросить AICC", systemImage: "sparkles") {}.accessibilityLabel("Спросить AICC") } }
        }
    }
}

private struct CalmStatus: View {
    let freshness: Freshness
    let needsAttention: Int
    let connection: AICCAppModel.ConnectionState
    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: needsAttention == 0 ? "checkmark.circle.fill" : "eye.circle.fill")
                .font(.system(size: 31)).foregroundStyle(needsAttention == 0 ? AICCTheme.forest : .orange)
            VStack(alignment: .leading) {
                Text(needsAttention == 0 ? "Сейчас всё спокойно" : "Есть один вопрос на будущее").font(.headline)
                Text(freshness == .offline ? "Показаны последние доступные данные" : connection.title).foregroundStyle(.secondary)
            }
        }
        .padding().frame(maxWidth: .infinity, alignment: .leading)
        .background(needsAttention == 0 ? AICCTheme.mint : AICCTheme.peach, in: RoundedRectangle(cornerRadius: 20))
        .accessibilityElement(children: .combine)
    }
}

private struct ProgressCard: View {
    let goal: WaveGoal?

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            Text("БЛИЖАЙШАЯ ЦЕЛЬ").font(.caption2.weight(.bold)).tracking(1.1).foregroundStyle(.secondary)
            if let goal {
                Text(goal.title).font(.system(.title2, design: .serif, weight: .medium))
                Text(detail(goal)).foregroundStyle(.secondary)
                ProgressView(value: goal.progress).tint(AICCTheme.plum)
                    .accessibilityLabel("\(goal.title): завершено \(goal.done) из \(goal.total)")
            } else {
                Text("Цель появится\nвместе с данными").font(.system(.title2, design: .serif, weight: .medium))
                Text("Сервер ещё не передал картину программы.").foregroundStyle(.secondary)
            }
        }.padding(22).frame(maxWidth: .infinity, alignment: .leading).background(AICCTheme.lilac, in: RoundedRectangle(cornerRadius: 24))
    }

    private func detail(_ goal: WaveGoal) -> String {
        var parts = ["Завершено \(goal.done) из \(goal.total)"]
        if goal.inProgress > 0 { parts.append("в работе: \(goal.inProgress)") }
        if goal.review > 0 { parts.append("на проверке: \(goal.review)") }
        return parts.joined(separator: " · ")
    }
}

private struct ProjectRow: View {
    let name: String; let detail: String; let color: Color
    var body: some View {
        HStack { Circle().fill(color).frame(width: 10, height: 10); VStack(alignment: .leading) { Text(name).font(.headline); Text(detail).foregroundStyle(.secondary) }; Spacer(); Image(systemName: "chevron.right").foregroundStyle(.tertiary) }
            .padding().background(.background, in: RoundedRectangle(cornerRadius: 18)).overlay { RoundedRectangle(cornerRadius: 18).stroke(.quaternary) }
            .accessibilityElement(children: .combine)
    }
}

private struct WorkView: View {
    let tasks: [AICCNativeCore.Task]

    private var attention: [AICCNativeCore.Task] { tasks.filter { $0.blocker != nil } }
    private var active: [AICCNativeCore.Task] {
        tasks
            .filter { task in
                task.blocker == nil
                    && task.state != .done
                    && task.evidence.derivedStatus != .completed
            }
            .sorted { rank($0) < rank($1) }
    }

    // What moves today first: doing > reviewing > queued > someday.
    private func rank(_ task: AICCNativeCore.Task) -> Int {
        switch task.state {
        case .inProgress: 0
        case .review: 1
        case .next: 2
        case .backlog, .deferred: 3
        case .done: 4
        case nil: 2
        }
    }

    var body: some View {
        CompanionPage(title: "Работа", subtitle: "То, что движется сегодня. Без технических деталей.") {
            if tasks.isEmpty {
                CompanionCard(title: "Пока пусто", detail: "Задачи появятся, как только сервер передаст картину.", tint: .gray)
            }
            ForEach(attention.prefix(5)) { task in
                NavigationLink(value: task) {
                    CompanionCard(title: task.title, detail: task.blocker ?? "Нужно ваше внимание.", tint: .orange, badge: "Внимание")
                }.buttonStyle(.plain)
            }
            ForEach(active.prefix(20)) { task in
                NavigationLink(value: task) {
                    CompanionCard(title: task.title, detail: statusLine(for: task), tint: AICCTheme.plum)
                }.buttonStyle(.plain)
            }
        }
    }

    private func statusLine(for task: AICCNativeCore.Task) -> String {
        TaskStateText.line(for: task)
    }
}

enum TaskStateText {
    static func line(for task: AICCNativeCore.Task) -> String {
        if let state = task.state {
            switch state {
            case .backlog: return "В планах."
            case .next: return "Следующая в очереди."
            case .inProgress: return "В работе."
            case .review: return "На проверке."
            case .done: return "Завершена."
            case .deferred: return "Ждёт вашего решения."
            }
        }
        switch task.evidence.derivedStatus {
        case .inProgress: return "В работе."
        case .awaitingCI: return "Идут проверки."
        case .awaitingAcceptance: return "Ждёт приёмки."
        case .completed: return "Завершена."
        case .unknown: return "Состояние уточняется."
        }
    }
}

struct TaskDetailView: View {
    let task: AICCNativeCore.Task

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(task.title)
                    .font(.system(.largeTitle, design: .serif, weight: .medium))
                Text(TaskStateText.line(for: task))
                    .font(.title3).foregroundStyle(.secondary)
                if let blocker = task.blocker {
                    CompanionCard(title: "Что требуется", detail: blocker, tint: .orange, badge: "Внимание")
                }
                VStack(alignment: .leading, spacing: 9) {
                    Text("ХОД ДОСТАВКИ").font(.caption2.weight(.bold)).tracking(1).foregroundStyle(.secondary)
                    detailRow("Идентификатор", task.id)
                    detailRow("Проверки (CI)", evidenceText(task.evidence.ci))
                    detailRow("Приёмка", evidenceText(task.evidence.acceptance))
                    if let pr = task.evidence.pullRequest { detailRow("Изменение", pr) }
                    if let merged = task.evidence.mergedSHA { detailRow("Влито", String(merged.prefix(10))) }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(AICCTheme.lilac, in: RoundedRectangle(cornerRadius: 20))
                Spacer()
            }
            .padding()
        }
        .navigationTitle("Задача")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
    }

    private func detailRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value).multilineTextAlignment(.trailing)
        }
    }

    private func evidenceText(_ state: EvidenceState) -> String {
        switch state {
        case .unknown: "нет данных"
        case .observed: "замечены"
        case .verified: "пройдены"
        case .rejected: "отклонены"
        case .pending: "идут"
        }
    }
}

private struct DialoguesView: View {
    let dialogs: [DialogSummary]

    var body: some View {
        CompanionPage(title: "Диалоги", subtitle: "Разговоры всегда связаны с конкретным делом.") {
            if dialogs.isEmpty {
                CompanionCard(title: "Пока тихо", detail: "Разговоры появятся здесь, как только начнутся.", tint: .gray)
            }
            ForEach(dialogs.prefix(20)) { dialog in
                CompanionCard(
                    title: dialog.title,
                    detail: detailLine(dialog),
                    tint: AICCTheme.plum
                )
            }
        }
    }

    private func detailLine(_ dialog: DialogSummary) -> String {
        var parts: [String] = ["Сообщений: \(dialog.messageCount)"]
        if let last = dialog.lastActivityAt {
            let formatter = RelativeDateTimeFormatter()
            formatter.locale = Locale(identifier: "ru_RU")
            formatter.unitsStyle = .full
            parts.append("обновлён " + formatter.localizedString(for: last, relativeTo: .now))
        }
        return parts.joined(separator: " · ")
    }
}

private struct DecisionsView: View {
    var body: some View {
        CompanionPage(title: "Решения", subtitle: "Только важные выборы — с контекстом и ясными последствиями.") {
            VStack(alignment: .leading, spacing: 9) {
                Text("НА БУДУЩЕЕ").font(.caption2.weight(.bold)).tracking(1).foregroundStyle(.secondary)
                Text("Как провести первую проверку дизайна?").font(.system(.title2, design: .serif, weight: .medium))
                Text("Рекомендация готова. Решение можно спокойно отложить.").foregroundStyle(.secondary)
                Button("Посмотреть варианты") {}
                    .buttonStyle(.borderedProminent).tint(AICCTheme.plum).padding(.top, 4)
            }.padding(22).frame(maxWidth: .infinity, alignment: .leading).background(AICCTheme.peach, in: RoundedRectangle(cornerRadius: 24))
            CompanionCard(title: "Предыдущие решения", detail: "Все идут по плану.", tint: AICCTheme.forest)
        }
    }
}

private struct MoreView: View {
    let events: [TimelineEvent]
    let connection: AICCAppModel.ConnectionState
    var body: some View {
        CompanionPage(title: "Ещё", subtitle: "Всё остальное уже предусмотрено, но не мешает вам каждый день.") {
            CompanionCard(title: "Подключение", detail: connection.title, tint: connection == .offline ? .orange : AICCTheme.forest)
            CompanionCard(title: "Помощники и память", detail: "Команда AI, объяснения и успешные решения.", tint: AICCTheme.plum)
            CompanionCard(title: "Проверки и происшествия", detail: "Картина качества, рисков и восстановления.", tint: AICCTheme.forest)
            CompanionCard(title: "Сводки, совет и настройки", detail: "Всё для спокойной картины и управления.", tint: .gray)
        }
    }
}

private struct CompanionPage<Content: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder let content: Content

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text(title).font(.system(size: 44, weight: .medium, design: .serif)).tracking(-1)
                    Text(subtitle).font(.title3).foregroundStyle(.secondary).padding(.bottom, 12)
                    content
                }.padding()
            }
            .navigationTitle("AICC")
            .navigationDestination(for: AICCNativeCore.Task.self) { task in
                TaskDetailView(task: task)
            }
        }
    }
}

private struct CompanionCard: View {
    let title: String
    let detail: String
    let tint: Color
    var badge: String? = nil

    var body: some View {
        HStack(spacing: 14) {
            Circle().fill(tint).frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            if let badge { Text(badge).font(.caption2.weight(.bold)).foregroundStyle(tint) }
            Image(systemName: "chevron.right").foregroundStyle(.tertiary)
        }.padding(18).background(.background, in: RoundedRectangle(cornerRadius: 19)).overlay { RoundedRectangle(cornerRadius: 19).stroke(.quaternary) }
    }
}

#Preview("Спокойный обзор") { AICCNativeShell(model: AICCAppModel()) }
