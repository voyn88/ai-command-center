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
    var body: some View {
        CompanionPage(title: "Работа", subtitle: "То, что движется сегодня. Без технических деталей.") {
            CompanionCard(title: "AICC Native", detail: "Следующий шаг — посмотреть обновлённый дизайн.", tint: AICCTheme.plum)
            CompanionCard(title: "AIOS", detail: "План выполняется, критичных препятствий нет.", tint: AICCTheme.forest)
            CompanionCard(title: "План недели", detail: "Три важных направления в понятном порядке.", tint: .gray)
        }
    }
}

private struct DialoguesView: View {
    var body: some View {
        CompanionPage(title: "Диалоги", subtitle: "Разговоры всегда связаны с конкретным делом.") {
            CompanionCard(title: "Обсуждение дизайна AICC", detail: "Есть короткое резюме и следующий вопрос для вас.", tint: .orange, badge: "Новый")
            CompanionCard(title: "Ваш недельный бриф", detail: "Три главные темы, которые стоит знать.", tint: AICCTheme.plum)
        }
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
    var body: some View {
        CompanionPage(title: "Ещё", subtitle: "Всё остальное уже предусмотрено, но не мешает вам каждый день.") {
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
            }.navigationTitle("AICC")
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

#Preview("Спокойный обзор") { AICCNativeShell(snapshot: .preview) }
