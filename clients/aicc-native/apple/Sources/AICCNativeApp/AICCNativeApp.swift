import SwiftUI
import AICCNativeCore

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
    case overview, attention, projects, activity
    var id: Self { self }
    var title: String { switch self { case .overview: "Обзор"; case .attention: "Внимание"; case .projects: "Проекты"; case .activity: "События" } }
    var icon: String { switch self { case .overview: "sparkles"; case .attention: "bell"; case .projects: "square.grid.2x2"; case .activity: "clock.arrow.circlepath" } }
}

struct AICCNativeShell: View {
    let snapshot: Snapshot
    @State private var tab: AppTab = .overview

    var body: some View {
        TabView(selection: $tab) {
            OverviewView(snapshot: snapshot)
                .tabItem { Label(AppTab.overview.title, systemImage: AppTab.overview.icon) }.tag(AppTab.overview)
            AttentionView(snapshot: snapshot)
                .tabItem { Label(AppTab.attention.title, systemImage: AppTab.attention.icon) }.tag(AppTab.attention)
            ProjectsView()
                .tabItem { Label(AppTab.projects.title, systemImage: AppTab.projects.icon) }.tag(AppTab.projects)
            ActivityView(events: snapshot.events)
                .tabItem { Label(AppTab.activity.title, systemImage: AppTab.activity.icon) }.tag(AppTab.activity)
        }
        .tint(.indigo)
    }
}

private struct OverviewView: View {
    let snapshot: Snapshot

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Спокойная картина дня").font(.caption.weight(.bold)).foregroundStyle(.secondary)
                    Text("Всё под контролем.").font(.system(size: 38, weight: .bold, design: .rounded))
                    Text("Работа движется по плану. Автоматика сама разбирается с обычными вопросами.")
                        .font(.title3).foregroundStyle(.secondary)
                    CalmStatus(freshness: snapshot.freshness, needsAttention: snapshot.overview.needsAttention)
                    ProgressCard()
                    Text("Проекты").font(.title2.bold())
                    ProjectRow(name: "AIOS", detail: "Работа идёт по плану", color: .mint)
                    ProjectRow(name: "AICC", detail: "Новая версия проходит проверку", color: .indigo)
                    ProjectRow(name: "Другие проекты", detail: "Ничего важного не происходит", color: .gray)
                }.padding()
            }
            .navigationTitle("AICC")
            .toolbar { ToolbarItem(placement: .primaryAction) { Button("Обновить", systemImage: "arrow.clockwise") {}.accessibilityLabel("Обновить данные") } }
        }
    }
}

private struct CalmStatus: View {
    let freshness: Freshness
    let needsAttention: Int
    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: needsAttention == 0 ? "checkmark.circle.fill" : "eye.circle.fill")
                .font(.system(size: 35)).foregroundStyle(needsAttention == 0 ? .green : .orange)
            VStack(alignment: .leading) {
                Text(needsAttention == 0 ? "Всё идёт ровно" : "Есть один вопрос на будущее").font(.headline)
                Text(freshness == .offline ? "Показаны последние доступные данные" : "Ничего срочного не требует вашего решения").foregroundStyle(.secondary)
            }
        }
        .padding().frame(maxWidth: .infinity, alignment: .leading)
        .background(needsAttention == 0 ? Color.green.opacity(0.11) : Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 20))
        .accessibilityElement(children: .combine)
    }
}

private struct ProgressCard: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            Text("Ближайшая цель").font(.headline)
            Text("Работа почти готова").font(.title3.bold())
            Text("Осталась финальная проверка. Мы сообщим, только если понадобится ваше участие.").foregroundStyle(.secondary)
            ProgressView(value: 0.72).tint(.indigo).accessibilityLabel("Ближайшая цель почти готова")
        }.padding().background(.indigo.opacity(0.09), in: RoundedRectangle(cornerRadius: 20))
    }
}

private struct ProjectRow: View {
    let name: String; let detail: String; let color: Color
    var body: some View {
        HStack { Circle().fill(color).frame(width: 10, height: 10); VStack(alignment: .leading) { Text(name).font(.headline); Text(detail).foregroundStyle(.secondary) }; Spacer(); Image(systemName: "chevron.right").foregroundStyle(.tertiary) }
            .padding().background(.background, in: RoundedRectangle(cornerRadius: 16)).overlay { RoundedRectangle(cornerRadius: 16).stroke(.quaternary) }
            .accessibilityElement(children: .combine)
    }
}

private struct AttentionView: View {
    let snapshot: Snapshot
    var body: some View { NavigationStack { List { Section("Требует взгляда") { if snapshot.overview.needsAttention == 0 { ContentUnavailableView("Всё спокойно", systemImage: "checkmark.circle", description: Text("Сейчас нет вопросов, требующих вашего решения.")) } else { Label("Система собирает контекст для следующего решения", systemImage: "lightbulb") } } }.navigationTitle("Внимание") } }
}

private struct ProjectsView: View {
    var body: some View { NavigationStack { ContentUnavailableView("Картина по проектам", systemImage: "square.grid.2x2", description: Text("Здесь появится понятная сводка по каждому направлению.")) .navigationTitle("Проекты") } }
}

private struct ActivityView: View {
    let events: [TimelineEvent]
    var body: some View { NavigationStack { List(events) { event in Label(event.summary, systemImage: "circle.fill").foregroundStyle(.secondary) }.navigationTitle("События") } }
}

#Preview("Спокойный обзор") { AICCNativeShell(snapshot: .preview) }
