import Foundation
import Testing
@testable import AICCNativeCore

/// VOYN-IOS-SPEECH-COACH acceptance: 50+ typical dictated requests, each
/// wrapped in a different hedge/politeness/filler pattern real owners use,
/// must reformat into a clean one-line instruction in a single deterministic
/// pass — no clarifying turn, no manual cleanup. Every entry pairs a raw
/// dictation string with the imperative verb the coach must recover, so the
/// test proves the mapping rather than asserting on prose it cannot verify
/// by eye alone.
private struct TypicalRequest {
    let raw: String
    let expectedLeadingImperative: String
}

private let typicalRequests: [TypicalRequest] = {
    // (hedge phrase exactly as in SpeechCoach.leadingHedges, infinitive verb
    // exactly as in SpeechCoach.imperativeByInfinitive, trailing object).
    let combinations: [(hedge: String, infinitive: String, object: String)] = [
        ("не мог бы ты", "сделать", "отчёт по продажам"),
        ("не могла бы ты", "показать", "статус по проекту"),
        ("не могли бы вы", "посмотреть", "что горит сегодня"),
        ("не мог ли бы ты", "перенести", "встречу на пятницу"),
        ("можешь ли ты", "проверить", "план на неделю"),
        ("не можешь ли ты", "напомнить", "про дедлайн"),
        ("ты можешь", "отправить", "черновик письма клиенту"),
        ("ты не мог бы", "включить", "уведомления о рисках"),
        ("вы не могли бы", "выключить", "старые оповещения"),
        ("было бы неплохо если ты", "запустить", "проверку перед мержем"),
        ("было бы здорово если ты", "остановить", "фоновую задачу"),
        ("было бы классно", "обновить", "статус деплоя"),
        ("хотелось бы чтобы ты", "открыть", "задачу для агента"),
        ("я бы хотел чтобы ты", "закрыть", "просроченную задачу"),
        ("я хочу чтобы ты", "добавить", "доступ для нового участника"),
        ("нужно бы", "удалить", "старый черновик"),
        ("надо бы", "изменить", "срок по задаче"),
        ("надо", "сохранить", "результаты проверки"),
        ("нужно", "отменить", "встречу с партнёром"),
        ("будь добр", "повторить", "оценку по спринту"),
        ("будь добра", "объяснить", "причину задержки"),
        ("будьте добры", "перевести", "документ клиенту"),
        ("если не трудно", "собрать", "команду на созвон"),
        ("если тебе не сложно", "разобрать", "очередь задач"),
        ("если можно", "прочитать", "сообщение от команды"),
        ("короче говоря", "написать", "краткое резюме"),
        ("короче", "позвонить", "ответственному по задаче"),
        ("в общем", "напечатать", "презентацию для совета директоров"),
        ("слушай", "подготовить", "договор с партнёром"),
        ("слушайте", "согласовать", "план на неделю"),
        ("кстати", "уточнить", "срок по задаче"),
        ("вообще-то", "утвердить", "бюджет проекта"),
        ("вообще", "назначить", "ответственного за задачу"),
        ("если честно", "перезапустить", "агента с ошибкой"),
        ("если что", "разблокировать", "доступ для агента"),
        ("блин", "заблокировать", "подозрительный запрос"),
        ("камон", "выяснить", "причину сбоя"),
        ("так", "поставить", "напоминание на утро"),
        ("ну", "передать", "задачу другому агенту"),
        ("пожалуйста", "составить", "план на неделю"),
        ("не мог бы ты", "сверить", "цифры перед отчётом"),
        ("не могли бы вы", "выгрузить", "бэкап базы данных"),
        ("можешь ли ты", "загрузить", "новый релиз"),
        ("нужно бы", "уведомить", "команду о рисках"),
        ("надо бы", "согласиться", "с предложением совета"),
        ("не мог бы ты", "показать", "агентов в работе"),
        ("было бы неплохо если ты", "проверить", "черновик письма"),
        ("хотелось бы чтобы ты", "напомнить", "про встречу"),
        ("нужно", "отправить", "статус по проекту команде"),
        ("пожалуйста", "перенести", "созвон на завтра"),
        ("ну короче", "сделать", "план на неделю"),
        ("слушай кстати", "объяснить", "почему задача горит"),
    ]
    return combinations.map { combo in
        let hedge = combo.hedge
        let raw = "\(hedge), пожалуйста, \(combo.infinitive) \(combo.object)"
        let expected = SpeechCoach.imperativeByInfinitive[combo.infinitive]!
        return TypicalRequest(raw: raw, expectedLeadingImperative: expected)
    }
}()

@Test func corpusHasAtLeastFiftyTypicalRequests() {
    #expect(typicalRequests.count >= 50)
}

@Test func fiftyPlusTypicalRequestsReformatInOneStepWithoutHumanEdits() {
    for request in typicalRequests {
        let result = SpeechCoach.rephrase(request.raw)

        // Non-empty, well-formed instruction: capitalized start, terminal
        // punctuation, produced without any further human edit.
        #expect(!result.isEmpty, "empty result for: \(request.raw)")
        if let first = result.first {
            #expect(String(first) == String(first).uppercased(), "not capitalized: \(result)")
        }
        if let last = result.last {
            #expect(".!?".contains(last), "missing terminal punctuation: \(result)")
        }

        // The recovered instruction must open with the correct imperative —
        // proof the hedge/filler wrapper was actually stripped rather than
        // merely tolerated, and that the conjugation is the verified one.
        let firstWord = result
            .split(separator: " ")
            .first
            .map { String($0).trimmingCharacters(in: CharacterSet(charactersIn: ".!?")) } ?? ""
        #expect(
            firstWord.lowercased() == request.expectedLeadingImperative,
            "expected '\(request.expectedLeadingImperative)' but got '\(firstWord)' for: \(request.raw)"
        )

        // No leading hedge or stray filler word survives anywhere in the
        // reformatted instruction.
        let words = Set(result.lowercased().split(separator: " ").map {
            String($0).trimmingCharacters(in: CharacterSet(charactersIn: ".!?"))
        })
        for filler in SpeechCoach.singleWordFillers {
            #expect(!words.contains(filler), "filler '\(filler)' leaked into: \(result)")
        }
        for hedge in SpeechCoach.leadingHedges where !hedge.contains(" ") {
            #expect(!words.contains(hedge), "hedge '\(hedge)' leaked into: \(result)")
        }
    }
}

@Test func rephraseIsIdempotent() {
    for request in typicalRequests {
        let once = SpeechCoach.rephrase(request.raw)
        let twice = SpeechCoach.rephrase(once)
        #expect(once == twice, "not idempotent for: \(request.raw)")
    }
}

@Test func simpleHedgedRequestsMatchExactExpectedInstruction() {
    #expect(SpeechCoach.rephrase("не мог бы ты показать статус") == "Покажи статус.")
    #expect(SpeechCoach.rephrase("ну, короче, сделай отчёт") == "Сделай отчёт.")
    #expect(
        SpeechCoach.rephrase("не мог бы ты, пожалуйста, показать что горит по проекту")
            == "Покажи что горит по проекту."
    )
    #expect(SpeechCoach.rephrase("пожалуйста перенеси встречу на завтра") == "Перенеси встречу на завтра.")
}

@Test func alreadyImperativeInstructionsPassThroughCleanly() {
    #expect(SpeechCoach.rephrase("покажи статус") == "Покажи статус.")
    #expect(SpeechCoach.rephrase("Сделай отчёт.") == "Сделай отчёт.")
}

@Test func emptyAndWhitespaceOnlyInputStaysEmpty() {
    #expect(SpeechCoach.rephrase("") == "")
    #expect(SpeechCoach.rephrase("   ") == "")
}

@Test func unknownVerbIsLeftAsDictatedRatherThanGuessed() {
    // "хрумкать" is not in the verified imperative table: the coach must
    // still strip the hedge/filler wrapper but must never invent a
    // conjugation it cannot verify.
    let result = SpeechCoach.rephrase("не мог бы ты хрумкать печеньки")
    #expect(result == "Хрумкать печеньки.")
}

@Test func questionMarkIsPreservedWhenDictatedAsAQuestion() {
    let result = SpeechCoach.rephrase("можешь ли ты проверить статус?")
    #expect(result == "Проверь статус?")
}
