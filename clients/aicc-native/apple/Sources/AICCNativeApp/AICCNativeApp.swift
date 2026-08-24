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
    var body: some Scene {
        WindowGroup { AICCNativeShell(snapshot: (try? Fixture.healthySnapshot()) ?? .preview) }
    }
}

private extension Snapshot {
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
    let snapshot: Snapshot
    @State private var tab: AppTab = .overview

    var body: some View {
        TabView(selection: $tab) {
            OverviewView(snapshot: snapshot)
                .tabItem { Label(AppTab.overview.title, systemImage: AppTab.overview.icon) }.tag(AppTab.overview)
            WorkView()
                .tabItem { Label(AppTab.work.title, systemImage: AppTab.work.icon) }.tag(AppTab.work)
            DialoguesView()
                .tabItem { Label(AppTab.dialogues.title, systemImage: AppTab.dialogues.icon) }.tag(AppTab.dialogues)
            DecisionsView()
                .tabItem { Label(AppTab.decisions.title, systemImage: AppTab.decisions.icon) }.tag(AppTab.decisions)
            MoreView(events: snapshot.events)
                .tabItem { Label(AppTab.more.title, systemImage: AppTab.more.icon) }.tag(AppTab.more)
        }
        .tint(AICCTheme.plum)
    }
}

private struct OverviewView: View {
    let snapshot: Snapshot

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("ДОБРОЕ УТРО").font(.caption2.weight(.bold)).tracking(1.3).foregroundStyle(AICCTheme.plum)
                    Text("Всё идёт\nсвоим ходом.").font(.system(size: 46, weight: .medium, design: .serif)).tracking(-1.5)
                    Text("Я собрала главное и оставила вам только то, что действительно заслуживает внимания.")
                        .font(.title3).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    CalmStatus(freshness: snapshot.freshness, needsAttention: snapshot.overview.needsAttention)
                    ProgressCard()
                    Text("Ваши проекты").font(.title2.weight(.semibold))
                    ProjectRow(name: "AIOS", detail: "Главная работа идёт по плану", color: AICCTheme.forest)
                    ProjectRow(name: "AICC Native", detail: "Собираем новый опыт для Mac и iPhone", color: AICCTheme.plum)
                    ProjectRow(name: "Ваш портфель", detail: "Ничего не требует срочного участия", color: .gray)
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
    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: needsAttention == 0 ? "checkmark.circle.fill" : "eye.circle.fill")
                .font(.system(size: 31)).foregroundStyle(needsAttention == 0 ? AICCTheme.forest : .orange)
            VStack(alignment: .leading) {
                Text(needsAttention == 0 ? "Сейчас всё спокойно" : "Есть один вопрос на будущее").font(.headline)
                Text(freshness == .offline ? "Показаны последние доступные данные" : "Ничего срочного не требует вашего решения").foregroundStyle(.secondary)
            }
        }
        .padding().frame(maxWidth: .infinity, alignment: .leading)
        .background(needsAttention == 0 ? AICCTheme.mint : AICCTheme.peach, in: RoundedRectangle(cornerRadius: 20))
        .accessibilityElement(children: .combine)
    }
}

private struct ProgressCard: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            Text("БЛИЖАЙШАЯ ЦЕЛЬ").font(.caption2.weight(.bold)).tracking(1.1).foregroundStyle(.secondary)
            Text("Новая версия AICC\nпочти готова").font(.system(.title2, design: .serif, weight: .medium))
            Text("Осталась финальная проверка. Мы сообщим, только если понадобится ваше участие.").foregroundStyle(.secondary)
            ProgressView(value: 0.72).tint(AICCTheme.plum).accessibilityLabel("Ближайшая цель почти готова")
        }.padding(22).background(AICCTheme.lilac, in: RoundedRectangle(cornerRadius: 24))
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
    var body: some View { NavigationStack { List { Section("Сегодня") { Label("AICC Native — финальная проверка макета", systemImage: "circle.fill").foregroundStyle(.indigo); Label("AIOS — работа идёт по плану", systemImage: "circle.fill").foregroundStyle(.green) }; Section("План") { Label("Три приоритета на эту неделю", systemImage: "calendar") } }.navigationTitle("Работа") } }
}

private struct DialoguesView: View {
    var body: some View { NavigationStack { List { Section("Нужен ответ") { Label("Обсуждение дизайна AICC Native", systemImage: "bubble.left.and.bubble.right.fill").foregroundStyle(.indigo) }; Section("Недавние") { Label("Еженедельный бриф", systemImage: "text.bubble"); Label("Выбор сценария для аудита", systemImage: "text.bubble") } }.navigationTitle("Диалоги").toolbar { Button("Спросить AICC", systemImage: "waveform") {} } } }
}

private struct DecisionsView: View {
    var body: some View { NavigationStack { List { Section("На будущее") { Label("Как провести первую проверку дизайна", systemImage: "lightbulb.fill").foregroundStyle(.orange) }; Section("Спокойно") { Label("Все предыдущие решения идут по плану", systemImage: "checkmark.circle") } }.navigationTitle("Решения") } }
}

private struct MoreView: View {
    let events: [TimelineEvent]
    var body: some View { NavigationStack { List { Section("Контроль") { Label("Проверки и происшествия", systemImage: "checkmark.shield"); Label("События", systemImage: "clock.arrow.circlepath") }; Section("Интеллект") { Label("Помощники и память", systemImage: "brain"); Label("Сводки и совет", systemImage: "doc.text") }; Section("Личное") { Label("Настройки", systemImage: "gearshape") }; if !events.isEmpty { Section("Недавнее") { ForEach(events) { Label($0.summary, systemImage: "circle.fill").foregroundStyle(.secondary) } } } }.navigationTitle("Ещё") } }
}

#Preview("Спокойный обзор") { AICCNativeShell(snapshot: .preview) }
