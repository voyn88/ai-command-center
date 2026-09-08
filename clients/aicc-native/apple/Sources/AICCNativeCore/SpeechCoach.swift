import Foundation

/// VOYN-IOS-SPEECH-COACH: a single-step, fully on-device text normalizer that
/// turns a spoken, free-form phrasing — "не мог бы ты, пожалуйста, ну как бы,
/// посмотреть что горит по проекту" — into one clear instruction — "Посмотри,
/// что горит по проекту." — in a single deterministic pass.
///
/// This never calls the gateway, never mutates AIOS state and never invokes a
/// remote model: it only reshapes text the owner is about to read or send,
/// which is why it can run offline and stay outside the Command Gateway
/// authorization boundary described in ADR-0001. It intentionally covers the
/// vocabulary of the product's typical requests (task/ops verbs) rather than
/// attempting open-ended natural-language understanding: outside that
/// vocabulary it strips hedges and fillers but never guesses a conjugation it
/// cannot verify, so it degrades to "cleaned up" rather than "wrong".
public enum SpeechCoach {

    /// Leading conversational hedges the owner's dictation commonly opens
    /// with. Stripped repeatedly — a hedge can stack, e.g. "ну слушай, не мог
    /// бы ты..." — until none remain, leaving the direct instruction.
    /// Matched as whole leading phrases so "надо" does not also eat the "б"
    /// of "надобно" or similar look-alikes.
    static let leadingHedges: [String] = [
        "не мог бы ты", "не могла бы ты", "не могли бы вы", "не мог ли бы ты",
        "можешь ли ты", "не можешь ли ты", "ты можешь", "ты не мог бы",
        "вы не могли бы", "было бы неплохо если ты", "было бы здорово если ты",
        "было бы классно", "хотелось бы чтобы ты", "я бы хотел чтобы ты",
        "я хочу чтобы ты", "нужно бы", "надо бы", "надо", "нужно",
        "будь добр", "будь добра", "будьте добры", "если не трудно",
        "если тебе не сложно", "если можно", "короче говоря", "короче",
        "в общем", "слушай", "слушайте", "кстати", "вообще-то", "вообще",
        "если честно", "если что", "блин", "камон", "так", "ну", "пожалуйста",
    ]

    /// Multi-word filler phrases removed wherever they appear mid-sentence.
    static let multiWordFillers: [String] = [
        "как бы", "так сказать", "в принципе", "это самое", "то есть",
    ]

    /// Single-word fillers removed wherever they appear mid-sentence (they
    /// are also covered above as *leading* hedges, but dictation frequently
    /// drops them in the middle instead: "покажи пожалуйста статус").
    static let singleWordFillers: Set<String> = [
        "пожалуйста", "типа", "короче", "вообще", "блин", "слушай", "ну", "собственно",
    ]

    /// Infinitive → second-person singular imperative, covering the verbs
    /// that appear in the product's reference set of typical requests
    /// (task/ops vocabulary: show, move, check, remind, start, stop, ...).
    static let imperativeByInfinitive: [String: String] = [
        "сделать": "сделай", "показать": "покажи", "посмотреть": "посмотри",
        "перенести": "перенеси", "проверить": "проверь", "напомнить": "напомни",
        "отправить": "отправь", "включить": "включи", "выключить": "выключи",
        "запустить": "запусти", "остановить": "останови", "обновить": "обнови",
        "открыть": "открой", "закрыть": "закрой", "добавить": "добавь",
        "удалить": "удали", "изменить": "измени", "сохранить": "сохрани",
        "отменить": "отмени", "повторить": "повтори", "объяснить": "объясни",
        "перевести": "переведи", "собрать": "собери", "разобрать": "разбери",
        "прочитать": "прочитай", "написать": "напиши", "позвонить": "позвони",
        "напечатать": "напечатай", "подготовить": "подготовь",
        "согласовать": "согласуй", "уточнить": "уточни", "утвердить": "утверди",
        "назначить": "назначь", "перезапустить": "перезапусти",
        "разблокировать": "разблокируй", "заблокировать": "заблокируй",
        "выяснить": "выясни", "поставить": "поставь", "передать": "передай",
        "составить": "составь", "сверить": "сверь", "выгрузить": "выгрузи",
        "загрузить": "загрузи", "уведомить": "уведоми", "согласиться": "согласись",
    ]

    /// Turns one dictated phrase into one clear instruction, in a single
    /// deterministic pass: no clarifying question, no retry, no human edit.
    /// Idempotent: `rephrase(rephrase(x)) == rephrase(x)`.
    public static func rephrase(_ raw: String) -> String {
        var text = collapseWhitespace(raw)
        guard !text.isEmpty else { return text }

        // Speech-to-text commas are cosmetic pauses, not grammar the owner
        // dictated on purpose; dropping them lets hedge/filler matching stay
        // a single plain-word pass instead of tracking punctuation variants.
        text = text.replacingOccurrences(of: ",", with: " ")
        text = collapseWhitespace(text)
        guard !text.isEmpty else { return text }

        text = stripLeadingHedges(text)
        guard !text.isEmpty else { return text }

        text = replaceLeadingInfinitiveWithImperative(text)
        text = removeMidSentenceFillers(text)
        text = collapseWhitespace(text)
        guard !text.isEmpty else { return text }

        text = capitalizeFirstLetter(text)
        text = appendTerminalPunctuationIfMissing(text)
        return text
    }

    private static func collapseWhitespace(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
    }

    private static func stripLeadingHedges(_ text: String) -> String {
        var current = text
        let orderedHedges = leadingHedges.sorted { $0.count > $1.count }
        var changed = true
        while changed {
            changed = false
            let lowered = current.lowercased()
            for hedge in orderedHedges {
                if lowered == hedge {
                    current = ""
                    changed = true
                    break
                }
                if lowered.hasPrefix(hedge + " ") {
                    current = String(current.dropFirst(hedge.count + 1))
                    current = current.trimmingCharacters(in: .whitespaces)
                    changed = true
                    break
                }
            }
        }
        return current
    }

    private static func replaceLeadingInfinitiveWithImperative(_ text: String) -> String {
        guard !text.isEmpty else { return text }
        let separatorIndex = text.firstIndex(of: " ") ?? text.endIndex
        let firstWord = String(text[text.startIndex..<separatorIndex]).lowercased()
        guard let imperative = imperativeByInfinitive[firstWord] else { return text }
        return imperative + String(text[separatorIndex...])
    }

    private static func removeMidSentenceFillers(_ text: String) -> String {
        var working = text
        for phrase in multiWordFillers {
            let pattern = "(?i)\\b" + NSRegularExpression.escapedPattern(for: phrase) + "\\b"
            working = working.replacingOccurrences(of: pattern, with: " ", options: .regularExpression)
        }
        let words = working.split(separator: " ").map(String.init)
        let kept = words.filter { !singleWordFillers.contains($0.lowercased()) }
        return kept.joined(separator: " ")
    }

    private static func capitalizeFirstLetter(_ text: String) -> String {
        guard let first = text.first else { return text }
        return String(first).uppercased() + String(text.dropFirst())
    }

    private static func appendTerminalPunctuationIfMissing(_ text: String) -> String {
        guard let last = text.last else { return text }
        if ".!?".contains(last) { return text }
        return text + "."
    }
}
