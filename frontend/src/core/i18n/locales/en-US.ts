import {
  CompassIcon,
  GraduationCapIcon,
  ImageIcon,
  MicroscopeIcon,
  PenLineIcon,
  ShapesIcon,
  SparklesIcon,
  VideoIcon,
} from "lucide-react";

import type { Translations } from "./types";

export const enUS: Translations = {
  // Locale meta
  locale: {
    localName: "English",
  },

  // Common
  common: {
    home: "Home",
    settings: "Settings",
    delete: "Delete",
    edit: "Edit",
    rename: "Rename",
    share: "Share",
    openInNewWindow: "Open in new window",
    close: "Close",
    more: "More",
    search: "Search",
    loadMore: "Load more",
    download: "Download",
    thinking: "Thinking",
    artifacts: "Artifacts",
    public: "Public",
    custom: "Custom",
    notAvailableInDemoMode: "Not available in demo mode",
    loading: "Loading...",
    version: "Version",
    lastUpdated: "Last updated",
    code: "Code",
    preview: "Preview",
    cancel: "Cancel",
    save: "Save",
    install: "Install",
    create: "Create",
    import: "Import",
    export: "Export",
    exportAsMarkdown: "Export as Markdown",
    exportAsJSON: "Export as JSON",
    exportSuccess: "Conversation exported",
  },

  // Home
  home: {
    docs: "Docs",
    blog: "Blog",
  },

  // Welcome
  welcome: {
    greeting: "Welcome to DeerNexus",
    description:
      "The enterprise-grade multi-tenant Agent OS. Org isolation · auditable operations · versioned releases. Orchestrate every agent production scenario safely.",

    createYourOwnSkill: "Create Your Own Skill",
    createYourOwnSkillDescription:
      "Create your own skill to unlock the power of DeerNexus. With customized skills,\nDeerNexus can help you search the web, analyze data, and generate\nartifacts like slides, web pages, and more.",
  },

  // Clipboard
  clipboard: {
    copyToClipboard: "Copy to clipboard",
    copiedToClipboard: "Copied to clipboard",
    failedToCopyToClipboard: "Failed to copy to clipboard",
    linkCopied: "Link copied to clipboard",
  },

  // Input Box
  inputBox: {
    placeholder: "How can I assist you today?",
    createSkillPrompt:
      "We're going to build a new skill step by step with `skill-creator`. To start, what do you want this skill to do?",
    addAttachments: "Add attachments",
    mode: "Mode",
    flashMode: "Flash",
    flashModeDescription: "Fast and efficient, but may not be accurate",
    reasoningMode: "Reasoning",
    reasoningModeDescription:
      "Reasoning before action, balance between time and accuracy",
    proMode: "Pro",
    proModeDescription:
      "Reasoning, planning and executing, get more accurate results, may take more time",
    ultraMode: "Ultra",
    ultraModeDescription:
      "Pro mode with subagents to divide work; best for complex multi-step tasks",
    reasoningEffort: "Reasoning Effort",
    reasoningEffortMinimal: "Minimal",
    reasoningEffortMinimalDescription: "Retrieval + Direct Output",
    reasoningEffortLow: "Low",
    reasoningEffortLowDescription: "Simple Logic Check + Shallow Deduction",
    reasoningEffortMedium: "Medium",
    reasoningEffortMediumDescription:
      "Multi-layer Logic Analysis + Basic Verification",
    reasoningEffortHigh: "High",
    reasoningEffortHighDescription:
      "Full-dimensional Logic Deduction + Multi-path Verification + Backward Check",
    searchModels: "Search models...",
    surpriseMe: "Surprise",
    surpriseMePrompt: "Surprise me",
    followupLoading: "Generating follow-up questions...",
    followupConfirmTitle: "Send suggestion?",
    followupConfirmDescription:
      "You already have text in the input. Choose how to send it.",
    followupConfirmAppend: "Append & send",
    followupConfirmReplace: "Replace & send",
    suggestions: [
      {
        suggestion: "Write",
        prompt: "Write a blog post about the latest trends on [topic]",
        icon: PenLineIcon,
      },
      {
        suggestion: "Research",
        prompt:
          "Conduct a deep dive research on [topic], and summarize the findings.",
        icon: MicroscopeIcon,
      },
      {
        suggestion: "Collect",
        prompt: "Collect data from [source] and create a report.",
        icon: ShapesIcon,
      },
      {
        suggestion: "Learn",
        prompt: "Learn about [topic] and create a tutorial.",
        icon: GraduationCapIcon,
      },
    ],
    suggestionsCreate: [
      {
        suggestion: "Webpage",
        prompt: "Create a webpage about [topic]",
        icon: CompassIcon,
      },
      {
        suggestion: "Image",
        prompt: "Create an image about [topic]",
        icon: ImageIcon,
      },
      {
        suggestion: "Video",
        prompt: "Create a video about [topic]",
        icon: VideoIcon,
      },
      {
        type: "separator",
      },
      {
        suggestion: "Skill",
        prompt:
          "We're going to build a new skill step by step with `skill-creator`. To start, what do you want this skill to do?",
        icon: SparklesIcon,
      },
    ],
  },

  // Sidebar
  sidebar: {
    newChat: "New chat",
    chats: "Chats",
    channels: "Channels",
    recentChats: "Recent chats",
    demoChats: "Demo chats",
    agents: "Agents",
  },

  // Agents
  agents: {
    title: "Agents",
    description:
      "Create and manage custom agents with specialized prompts and capabilities.",
    newAgent: "New Agent",
    emptyTitle: "No custom agents yet",
    emptyDescription:
      "Create your first custom agent with a specialized system prompt.",
    chat: "Chat",
    delete: "Delete",
    deleteConfirm:
      "Are you sure you want to delete this agent? This action cannot be undone.",
    deleteSuccess: "Agent deleted",
    newChat: "New chat",
    createPageTitle: "Design your Agent",
    createPageSubtitle:
      "Describe the agent you want — I'll help you create it through conversation.",
    nameStepTitle: "Name your new Agent",
    nameStepHint:
      "Letters, digits, and hyphens only — stored lowercase (e.g. code-reviewer)",
    nameStepPlaceholder: "e.g. code-reviewer",
    nameStepContinue: "Continue",
    nameStepInvalidError:
      "Invalid name — use only letters, digits, and hyphens",
    nameStepAlreadyExistsError: "An agent with this name already exists",
    nameStepNetworkError:
      "Network request failed — check your network or backend connection",
    nameStepCheckError: "Could not verify name availability — please try again",
    nameStepCheckErrorWithDetail: "Name check failed: {detail}",
    nameStepApiDisabledError:
      "Custom agent management is not enabled on this server. Please contact your administrator.",
    nameStepBootstrapMessage:
      "The new custom agent name is {name}. Help me design its purpose, behavior, and SOUL.md before saving it.",
    save: "Save agent",
    saving: "Saving agent...",
    saveRequested:
      "Save requested. DeerNexus is generating and saving an initial version now.",
    saveHint:
      "You can save this agent at any time from the top-right menu, even if this is only a first draft.",
    saveCommandMessage:
      "Please save this custom agent now based on everything we have discussed so far. Treat this as my explicit confirmation to save. If some details are still missing, make reasonable assumptions, generate a concise first SOUL.md in English, and call setup_agent immediately without asking me for more confirmation.",
    agentCreatedPendingRefresh:
      "The agent was created, but DeerNexus could not load it yet. Please refresh this page in a moment.",
    more: "More actions",
    agentCreated: "Agent created!",
    startChatting: "Start chatting",
    backToGallery: "Back to Gallery",
  },

  // Breadcrumb
  breadcrumb: {
    workspace: "Workspace",
    chats: "Chats",
  },

  // Workspace
  workspace: {
    officialWebsite: "DeerNexus's official website",
    githubTooltip: "DeerNexus on Github",
    settingsAndMore: "Settings and more",
    visitGithub: "DeerNexus on GitHub",
    reportIssue: "Report a issue",
    contactUs: "Contact us",
    about: "About DeerNexus",
    logout: "Log out",
    adminConsole: "Admin Console",
    studio: "Studio",
    gatewayUnavailable: "Gateway is temporarily unavailable.",
    gatewayUnavailableRetrying: "Retrying in the background…",
  },

  // Conversation
  conversation: {
    noMessages: "No messages yet",
    startConversation: "Start a conversation to see messages here",
  },

  // Chats
  chats: {
    searchChats: "Search chats",
    loadMoreToSearch: "Load more to search older conversations",
    loadingMore: "Loading more...",
    loadOlderChats: "Load older chats",
  },

  // Channels
  channels: {
    title: "Channels",
    connect: "Connect",
    modify: "Modify",
    reconnect: "Reconnect",
    disconnect: "Disconnect",
    connected: "Connected",
    notConnected: "Not connected",
    pending: "Pending",
    revoked: "Disconnected",
    disabled: "Disabled",
    unconfigured: "Not configured",
    unavailable: "Channel connections are unavailable right now.",
    unavailableShort: "Unavailable",
    setupTitle: (name: string) => `Connect ${name}`,
    setupEditTitle: (name: string) => `Modify ${name}`,
    setupDescription:
      "Enter the values needed by this server process. They are not written to config.yaml.",
    saveAndConnect: "Save and connect",
    saveChanges: "Save changes",
    descriptions: {
      telegram: "Telegram direct messages through your DeerNexus bot.",
      slack: "Slack workspace messages and mentions.",
      discord: "Discord server messages through your DeerNexus bot.",
      feishu: "Feishu and Lark messages through your DeerNexus app.",
      dingtalk: "DingTalk Stream Push messages through your DeerNexus bot.",
      wechat: "WeChat iLink messages through your DeerNexus bot.",
      wecom: "WeCom messages through your DeerNexus AI bot.",
    },
    connectedAs: (name: string) => `Connected as ${name}.`,
  },

  // Page titles (document title)
  pages: {
    appName: "DeerNexus",
    chats: "Chats",
    newChat: "New chat",
    untitled: "Untitled",
  },

  // Tool calls
  toolCalls: {
    moreSteps: (count: number) => `${count} more step${count === 1 ? "" : "s"}`,
    lessSteps: "Less steps",
    executeCommand: "Execute command",
    presentFiles: "Present files",
    needYourHelp: "Need your help",
    useTool: (toolName: string) => `Use "${toolName}" tool`,
    searchFor: (query: string) => `Search for "${query}"`,
    searchForRelatedInfo: "Search for related information",
    searchForRelatedImages: "Search for related images",
    searchForRelatedImagesFor: (query: string) =>
      `Search for related images for "${query}"`,
    searchOnWebFor: (query: string) => `Search on the web for "${query}"`,
    viewWebPage: "View web page",
    listFolder: "List folder",
    readFile: "Read file",
    writeFile: "Write file",
    clickToViewContent: "Click to view file content",
    writeTodos: "Update to-do list",
    skillInstallTooltip: "Install skill and make it available to DeerNexus",
  },

  // Subtasks
  uploads: {
    uploading: "Uploading...",
    uploadingFiles: "Uploading files, please wait...",
  },

  subtasks: {
    subtask: "Subtask",
    executing: (count: number) =>
      `Executing ${count === 1 ? "" : count + " "}subtask${count === 1 ? "" : "s in parallel"}`,
    in_progress: "Running subtask",
    completed: "Subtask completed",
    failed: "Subtask failed",
  },

  // Token Usage
  tokenUsage: {
    title: "Token Usage",
    label: "Tokens",
    input: "Input",
    output: "Output",
    total: "Total",
    view: "Display",
    unavailable:
      "No token usage yet. Usage appears only after a successful model response when the provider returns usage_metadata.",
    unavailableShort: "No usage returned",
    note: "Header totals use persisted thread usage, plus visible in-flight usage while a run is still streaming. Per-turn and debug usage come from currently visible messages only. Totals may differ from provider billing pages.",
    presets: {
      off: "Off",
      summary: "Summary",
      perTurn: "Per turn",
      debug: "Debug",
    },
    presetDescriptions: {
      off: "Hide token usage in the header and conversation.",
      summary: "Show only the current conversation total in the header.",
      perTurn:
        "Show the header total and one token summary per assistant turn.",
      debug: "Show the header total and step-level token debugging details.",
    },
    finalAnswer: "Final answer",
    stepTotal: "Step total",
    sharedAttribution: "Shared across multiple actions in this step",
    subagent: (description: string) => `Subagent: ${description}`,
    startTodo: (content: string) => `Start To-do: ${content}`,
    completeTodo: (content: string) => `Complete To-do: ${content}`,
    updateTodo: (content: string) => `Update To-do: ${content}`,
    removeTodo: (content: string) => `Remove To-do: ${content}`,
  },

  // Shortcuts
  shortcuts: {
    searchActions: "Search actions...",
    noResults: "No results found.",
    actions: "Actions",
    keyboardShortcuts: "Keyboard Shortcuts",
    keyboardShortcutsDescription:
      "Navigate DeerNexus faster with keyboard shortcuts.",
    openCommandPalette: "Open Command Palette",
    toggleSidebar: "Toggle Sidebar",
  },

  // Settings
  settings: {
    title: "Settings",
    description: "Adjust how DeerNexus looks and behaves for you.",
    sections: {
      account: "Account",
      appearance: "Appearance",
      channels: "Channels",
      memory: "Memory",
      tools: "Tools",
      skills: "Skills",
      notification: "Notification",
      models: "Models",
      about: "About",
    },
    models: {
      title: "Custom Model Providers",
      description:
        "Add private, OpenAI-compatible model providers. Providers you add here are visible only to you and can be selected in chat immediately.",
      addButton: "Add provider",
      editButton: "Edit",
      deleteButton: "Delete",
      emptyTitle: "No custom providers",
      emptyDescription:
        "Add a provider to use a custom endpoint and API key in your chats.",
      securityHint:
        "API keys are encrypted at rest and never sent back to the browser.",
      apiKeySet: "API key set",
      apiKeyUnset: "No API key",
      thinkingLabel: "Supports thinking",
      reasoningEffortLabel: "Supports reasoning effort",
      dialog: {
        createTitle: "Add model provider",
        editTitle: "Edit model provider",
        description:
          "Configure an OpenAI-compatible endpoint. The API key is stored encrypted.",
        nameLabel: "Provider name",
        namePlaceholder: "my-deepseek",
        nameHint: "Lowercase letters, numbers, and hyphens. Must be unique.",
        displayNameLabel: "Display name",
        displayNamePlaceholder: "My DeepSeek",
        descriptionLabel: "Description",
        descriptionPlaceholder: "Optional notes about this provider",
        modelLabel: "Model ID",
        modelPlaceholder: "deepseek-chat",
        baseUrlLabel: "Base URL",
        baseUrlPlaceholder: "https://api.deepseek.com/v1",
        apiKeyLabel: "API key",
        apiKeyPlaceholder: "sk-...",
        apiKeyEditHint: "Leave blank to keep the current key.",
        thinkingLabel: "Supports thinking",
        reasoningEffortLabel: "Supports reasoning effort",
        cancelButton: "Cancel",
        submitButton: "Add provider",
        submitButtonEditing: "Save changes",
      },
      validation: {
        nameRequired: "Provider name is required.",
        nameInvalid: "Use only lowercase letters, numbers, and hyphens.",
        modelRequired: "Model ID is required.",
        baseUrlRequired: "Base URL is required.",
        apiKeyRequired: "API key is required.",
      },
      deleteConfirm: "Delete this provider? This cannot be undone.",
      deleteSuccess: "Provider deleted",
      createSuccess: "Provider added",
      updateSuccess: "Provider updated",
    },
    memory: {
      title: "Memory",
      description:
        "DeerNexus automatically learns from your conversations in the background. These memories help DeerNexus understand you better and deliver a more personalized experience.",
      empty: "No memory data to display.",
      rawJson: "Raw JSON",
      exportButton: "Export memory",
      exportSuccess: "Memory exported",
      importButton: "Import memory",
      importConfirmTitle: "Import memory?",
      importConfirmDescription:
        "This will overwrite your current memory with the selected JSON backup.",
      importFileLabel: "Selected file",
      importInvalidFile:
        "Failed to read the selected memory file. Please choose a valid JSON export.",
      importSuccess: "Memory imported",
      manualFactSource: "Manual",
      addFact: "Add fact",
      addFactTitle: "Add memory fact",
      editFactTitle: "Edit memory fact",
      addFactSuccess: "Fact created",
      editFactSuccess: "Fact updated",
      clearAll: "Clear all memory",
      clearAllConfirmTitle: "Clear all memory?",
      clearAllConfirmDescription:
        "This will remove all saved summaries and facts. This action cannot be undone.",
      clearAllSuccess: "All memory cleared",
      factDeleteConfirmTitle: "Delete this fact?",
      factDeleteConfirmDescription:
        "This fact will be removed from memory immediately. This action cannot be undone.",
      factDeleteSuccess: "Fact deleted",
      factContentLabel: "Content",
      factCategoryLabel: "Category",
      factConfidenceLabel: "Confidence",
      factContentPlaceholder: "Describe the memory fact you want to save",
      factCategoryPlaceholder: "context",
      factConfidenceHint: "Use a number between 0 and 1.",
      factSave: "Save fact",
      factValidationContent: "Fact content cannot be empty.",
      factValidationConfidence: "Confidence must be a number between 0 and 1.",
      noFacts: "No saved facts yet.",
      summaryReadOnly:
        "Summary sections are read-only for now. You can currently add, edit, or delete individual facts, or clear all memory.",
      memoryFullyEmpty: "No memory saved yet.",
      factPreviewLabel: "Fact to delete",
      searchPlaceholder: "Search memory",
      filterAll: "All",
      filterFacts: "Facts",
      filterSummaries: "Summaries",
      noMatches: "No matching memory found.",
      markdown: {
        overview: "Overview",
        userContext: "User context",
        work: "Work",
        personal: "Personal",
        topOfMind: "Top of mind",
        historyBackground: "History",
        recentMonths: "Recent months",
        earlierContext: "Earlier context",
        longTermBackground: "Long-term background",
        updatedAt: "Updated at",
        facts: "Facts",
        empty: "(empty)",
        table: {
          category: "Category",
          confidence: "Confidence",
          confidenceLevel: {
            veryHigh: "Very high",
            high: "High",
            normal: "Normal",
            unknown: "Unknown",
          },
          content: "Content",
          source: "Source",
          createdAt: "CreatedAt",
          view: "View",
        },
      },
    },
    appearance: {
      themeTitle: "Theme",
      themeDescription:
        "Choose how the interface follows your device or stays fixed.",
      system: "System",
      light: "Light",
      dark: "Dark",
      systemDescription: "Match the operating system preference automatically.",
      lightDescription: "Bright palette with higher contrast for daytime.",
      darkDescription: "Dim palette that reduces glare for focus.",
      languageTitle: "Language",
      languageDescription: "Switch between languages.",
    },
    tools: {
      title: "Tools",
      description: "Manage the configuration and enabled status of MCP tools.",
      adminRequired: "Admin privileges are required to manage MCP tools.",
      empty: "No MCP tools configured.",
    },
    channels: {
      title: "Channels",
      description:
        "Connect IM accounts that can send messages to DeerNexus from outside the browser.",
      disabled:
        "Channel connections are not enabled on this server. Ask an administrator to enable channel_connections.",
    },
    skills: {
      title: "Agent Skills",
      description:
        "Manage the configuration and enabled status of the agent skills.",
      createSkill: "Create skill",
      emptyTitle: "No agent skill yet",
      emptyDescription:
        "Put your agent skill folders under the `/skills/custom` folder under the root folder of DeerNexus.",
      emptyButton: "Create Your First Skill",
    },
    notification: {
      title: "Notification",
      description:
        "DeerNexus only sends a completion notification when the window is not active. This is especially useful for long-running tasks so you can switch to other work and get notified when done.",
      requestPermission: "Request notification permission",
      deniedHint:
        "Notification permission was denied. You can enable it in your browser's site settings to receive completion alerts.",
      testButton: "Send test notification",
      testTitle: "DeerNexus",
      testBody: "This is a test notification.",
      notSupported: "Your browser does not support notifications.",
      disableNotification: "Disable notification",
    },
    account: {
      profileTitle: "Profile",
      email: "Email",
      role: "Role",
      changePasswordTitle: "Change Password",
      changePasswordDescription: "Update your account password.",
      currentPassword: "Current password",
      newPassword: "New password",
      confirmNewPassword: "Confirm new password",
      passwordMismatch: "New passwords do not match",
      passwordTooShort: "Password must be at least 8 characters",
      passwordChangedSuccess: "Password changed successfully",
      networkError: "Network error. Please try again.",
      updating: "Updating...",
      updatePassword: "Update Password",
      signOut: "Sign Out",
    },
    acknowledge: {
      emptyTitle: "Acknowledgements",
      emptyDescription: "Credits and acknowledgements will show here.",
    },
  },

  // Admin Console
  admin: {
    title: "Admin Console",
    backToWorkspace: "Back to Workspace",
    nav: {
      runs: "Runs",
      usage: "Usage",
      audit: "Failure / Audit",
    },
    runs: {
      title: "Runs",
      description:
        "All runs in your active Org. Use the filters to narrow by status or time window.",
      loadError: "Failed to load runs",
      unknownError: "Unknown error",
      loading: "Loading…",
      loadMore: "Load more",
      emptyTitle: "No runs in this window",
      emptyDescription: "Adjust the filters above or widen the time window.",
      columns: {
        runId: "Run ID",
        status: "Status",
        model: "Model",
        tokens: "Tokens",
        user: "User",
        created: "Created",
        error: "Error",
      },
    },
    usage: {
      title: "Usage",
      description:
        "Token consumption aggregated across all runs in your active Org.",
      loadError: "Failed to load usage",
      unknownError: "Unknown error",
      noCompletedRuns: "No completed runs in this window.",
      tooltipFormatter: (tokens: string, runs: number) =>
        `${tokens} tokens · ${runs} runs`,
      kpi: {
        totalTokens: "Total tokens",
        totalRuns: "Total runs",
        avgTokens: "Avg tokens / run",
        outputInputRatio: "Output : Input",
      },
      charts: {
        tokensByModel: "Tokens by model",
        tokensByModelDescription: 'Top 5 models; remaining grouped as "other".',
        tokensByCaller: "Tokens by caller",
        tokensByCallerDescription:
          "Lead agent vs subagent vs middleware breakdown.",
      },
      breakdown: {
        leadAgent: "Lead agent",
        subagent: "Subagent",
        middleware: "Middleware",
      },
    },
    audit: {
      title: "Failure / Audit",
      description:
        "Failures are derived from run status (error, timeout, interrupted). Structured audit events require PR-041 (Audit outbox) — until then this view is the operational failure surface.",
      failures24h: "Failures (last 24h)",
      failureRate: "Failure rate (window)",
      totalRuns24h: "Total runs (last 24h)",
      statsUnavailable: "Stats unavailable.",
      emptyTitle: (status: string) => `No ${status} runs in this window`,
      emptyDescription:
        "Adjust the time window above or pick another failure status.",
    },
    filter: {
      all: "All",
      status: "Status",
      allStatuses: "All statuses",
    },
  },

  // Studio (Agent artifact & release console)
  studio: {
    title: "Studio",
    backToWorkspace: "Back to Workspace",
    nav: {
      packages: "Packages",
      import: "Import",
    },
    packages: {
      title: "Agent Packages",
      description: "Manage agent artifacts, versions, and release channels.",
      importAgent: "Import agent",
      loadError: "Failed to load packages",
      loadErrorFallback:
        "The gateway may be unreachable, or you may lack studio permission.",
      emptyTitle: "No agent packages yet",
      emptyDescription:
        "Import an agent from the file-state layout to create its first package and version.",
      columns: {
        name: "Name",
        displayName: "Display name",
        status: "Status",
        created: "Created",
      },
    },
    detail: {
      loadErrorPackage: "Failed to load package.",
      loadErrorVersions: "Failed to load versions.",
      loadErrorChannels: "Failed to load channels.",
      tabs: {
        versions: "Versions",
        channels: "Channels",
        overview: "Overview",
      },
      newVersion: "New version",
      versionEmptyTitle: "No versions",
      versionEmptyDescription:
        "Import this agent from the file-state layout, or create a new version manually with the full manifest editor.",
      versionColumns: {
        version: "Version",
        digest: "Digest",
        status: "Status",
        size: "Size",
        created: "Created",
        actions: "Actions",
      },
      actions: {
        reviewing: "Reviewing…",
        review: "Review",
        publishing: "Publishing…",
        publish: "Publish",
        revoking: "Revoking…",
        revoke: "Revoke",
      },
      currentLabel: "Current: ",
      emptyPointer: "empty",
      promoteLabel: "Promote",
      rollbackLabel: "Rollback",
      selectVersionPlaceholder: "select version…",
      toLabel: " to",
      historyLabel: "History",
      byLabel: "by",
      systemActor: "system",
      promotePermTitle: (perm: string) =>
        `Requires ${perm} permission (admin only)`,
      promotePermTitleDev: (perm: string) => `Requires ${perm} permission`,
      rollbackPermTitle: (perm: string) =>
        `Requires ${perm} permission (admin only)`,
      metaTitle: "Package metadata",
      reconcile: "Reconcile inventory",
      reconciling: "Reconciling…",
      archive: "Archive package",
      archiving: "Archiving…",
      meta: {
        id: "ID",
        name: "Name",
        displayName: "Display name",
        description: "Description",
        status: "Status",
        workspace: "Workspace",
        createdBy: "Created by",
        createdAt: "Created at",
        updatedAt: "Updated at",
      },
    },
    importPage: {
      title: "Import agent",
      description:
        "Import an agent from the file-state layout (SOUL / config). The importer computes a digest and is idempotent: re-importing identical content returns the existing version instead of duplicating.",
      methodTitle: "File-state import",
      methodDescription:
        "Reads agents/{name}/ from the server-side agent directory. Requires the studio:package:write permission.",
      permissionHint:
        "You need the studio:package:write permission (org:admin or org:developer) to import agents. The form below is disabled.",
      successImported: "Imported",
      successIdempotent: "Idempotent re-import",
      successImportedDesc: "A new package + version were created.",
      successIdempotentDesc:
        "Identical content already imported — existing version returned.",
      labels: {
        agentDirName: "Agent directory name *",
        agentDirNameTitle: "Letters, digits, and hyphens only",
        version: "Version (SemVer) *",
        displayName: "Display name",
        displayNamePlaceholder: "defaults to name",
        userId: "User ID (optional)",
        userIdPlaceholder: "per-user agent dir",
        description: "Description",
        descriptionPlaceholder: "defaults to the agent config description",
      },
      importing: "Importing…",
      submit: "Import agent",
      meta: {
        package: "Package",
        version: "Version",
        status: "Status",
        digest: "Digest",
      },
    },
    newVersion: {
      title: "New version",
      back: "← Back",
      permissionHint:
        "You need the studio:package:write permission (org:admin or org:developer) to create versions.",
      basics: "Basics",
      basicsDescription:
        "Version (SemVer 2.0) and artifact content. The backend computes the digest over the content's UTF-8 bytes.",
      semverError:
        "Must be a valid SemVer 2.0.0 string (e.g. 1.0.0, 1.0.0-beta).",
      contentLabel: "Artifact content (UTF-8) *",
      contentPlaceholder:
        "Raw artifact payload — the agent definition / config / prompt.",
      manifestCore: "Manifest core",
      manifestCoreDescription:
        "Entry point and soul/prompt reference (ADR §3.3).",
      schemaVersion: "Schema version *",
      agentEntry: "Agent entry *",
      agentEntryPlaceholder: "e.g. soul",
      soulPrompt: "Soul / prompt ref",
      soulPromptPlaceholder:
        "Stable reference to the agent's soul/prompt (never plaintext secrets).",
      modelRequirements: "Model requirements",
      skills: "Skills",
      skillsDescription: "Stable name + optional version/digest (ADR §3.3).",
      tools: "Tools",
      mcpServers: "MCP servers",
      mcpServersDescription: "Stable id + optional version (ADR §3.3).",
      dependencies: "Dependencies",
      dependenciesDescription: "Explicit dependency locks (ADR §3.3).",
      networkRequirements: "Network requirements",
      networkRequirementsDescription:
        "Explicit network egress declarations (ADR §3.3).",
      secretRequirements: "Secret requirements",
      secretRequirementsDescription:
        "Secret references — name + ref only, never plaintext (ADR §3.3).",
      runtimeLimits: "Runtime limits",
      maxSteps: "Max steps",
      maxTokens: "Max tokens",
      timeout: "Timeout (s)",
      cancel: "Cancel",
      creating: "Creating…",
      submit: "Create version",
      addLabels: {
        model: "Add model",
        skill: "Add skill",
        tool: "Add tool",
        mcp: "Add MCP server",
        dependency: "Add dependency",
        network: "Add network requirement",
        secret: "Add secret ref",
      },
    },
    dynamicList: {
      removeRow: "Remove row",
    },
  },
};
