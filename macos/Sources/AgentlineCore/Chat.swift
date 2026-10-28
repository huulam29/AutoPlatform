import Foundation

/// GET /api/chat (agentline.chatlink): each linked chat platform and what it notifies about.
public struct ChatStatus: Decodable, Sendable, Equatable {
    public struct Platform: Codable, Sendable, Equatable {
        public struct Linked: Codable, Sendable, Equatable {
            public let chatId: String
            public let username: String?
            public let linkedAt: Double?

            enum CodingKeys: String, CodingKey {
                case username
                case chatId = "chat_id"
                case linkedAt = "linked_at"
            }

            public init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                // Telegram ids are numbers, Slack and Discord ids are strings
                if let n = try? c.decode(Int64.self, forKey: .chatId) {
                    chatId = String(n)
                } else {
                    chatId = try c.decode(String.self, forKey: .chatId)
                }
                username = try c.decodeIfPresent(String.self, forKey: .username)
                linkedAt = try c.decodeIfPresent(Double.self, forKey: .linkedAt)
            }
        }

        public struct Key: Codable, Sendable, Equatable {
            public let name: String
            public let set: Bool
        }

        public let title: String
        public let keys: [Key]
        public let tokenSet: Bool
        public let bot: String?
        public let linked: Linked?
        public let setup: String?

        enum CodingKeys: String, CodingKey {
            case title, keys, bot, linked, setup
            case tokenSet = "token_set"
        }
    }

    public struct Notify: Codable, Sendable, Equatable {
        public var gates: Bool
        public var failures: Bool
        public var done: Bool
        public var questions: Bool
    }

    public let platforms: [String]
    public let byName: [String: Platform]
    public let notify: Notify
    /// Set on POST /chat/{platform}/pair.
    public let code: String?
    public let link: String?

    private struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ s: String) { stringValue = s }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        platforms = try c.decodeIfPresent([String].self, forKey: Key("platforms")) ?? ["telegram"]
        var by: [String: Platform] = [:]
        for p in platforms {
            if let v = try c.decodeIfPresent(Platform.self, forKey: Key(p)) { by[p] = v }
        }
        byName = by
        notify = try c.decode(Notify.self, forKey: Key("notify"))
        code = try c.decodeIfPresent(String.self, forKey: Key("code"))
        link = try c.decodeIfPresent(String.self, forKey: Key("link"))
    }
}
