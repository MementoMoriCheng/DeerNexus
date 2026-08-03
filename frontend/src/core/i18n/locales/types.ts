import type { LucideIcon } from "lucide-react";

export interface Translations {
  // Locale meta
  locale: {
    localName: string;
  };

  // Common
  common: {
    home: string;
    settings: string;
    delete: string;
    edit: string;
    rename: string;
    share: string;
    openInNewWindow: string;
    close: string;
    more: string;
    search: string;
    loadMore: string;
    download: string;
    thinking: string;
    artifacts: string;
    public: string;
    custom: string;
    notAvailableInDemoMode: string;
    loading: string;
    version: string;
    lastUpdated: string;
    code: string;
    preview: string;
    cancel: string;
    save: string;
    install: string;
    create: string;
    import: string;
    export: string;
    exportAsMarkdown: string;
    exportAsJSON: string;
    exportSuccess: string;
  };

  home: {
    docs: string;
    blog: string;
  };

  // Welcome
  welcome: {
    greeting: string;
    description: string;
    createYourOwnSkill: string;
    createYourOwnSkillDescription: string;
  };

  // Clipboard
  clipboard: {
    copyToClipboard: string;
    copiedToClipboard: string;
    failedToCopyToClipboard: string;
    linkCopied: string;
  };

  // Input Box
  inputBox: {
    placeholder: string;
    createSkillPrompt: string;
    addAttachments: string;
    mode: string;
    flashMode: string;
    flashModeDescription: string;
    reasoningMode: string;
    reasoningModeDescription: string;
    proMode: string;
    proModeDescription: string;
    ultraMode: string;
    ultraModeDescription: string;
    reasoningEffort: string;
    reasoningEffortMinimal: string;
    reasoningEffortMinimalDescription: string;
    reasoningEffortLow: string;
    reasoningEffortLowDescription: string;
    reasoningEffortMedium: string;
    reasoningEffortMediumDescription: string;
    reasoningEffortHigh: string;
    reasoningEffortHighDescription: string;
    searchModels: string;
    surpriseMe: string;
    surpriseMePrompt: string;
    followupLoading: string;
    followupConfirmTitle: string;
    followupConfirmDescription: string;
    followupConfirmAppend: string;
    followupConfirmReplace: string;
    suggestions: {
      suggestion: string;
      prompt: string;
      icon: LucideIcon;
    }[];
    suggestionsCreate: (
      | {
          suggestion: string;
          prompt: string;
          icon: LucideIcon;
        }
      | {
          type: "separator";
        }
    )[];
  };

  // Sidebar
  sidebar: {
    recentChats: string;
    newChat: string;
    chats: string;
    demoChats: string;
    agents: string;
    channels: string;
  };

  // Agents
  agents: {
    title: string;
    description: string;
    newAgent: string;
    emptyTitle: string;
    emptyDescription: string;
    chat: string;
    delete: string;
    deleteConfirm: string;
    deleteSuccess: string;
    newChat: string;
    createPageTitle: string;
    createPageSubtitle: string;
    nameStepTitle: string;
    nameStepHint: string;
    nameStepPlaceholder: string;
    nameStepContinue: string;
    nameStepInvalidError: string;
    nameStepAlreadyExistsError: string;
    nameStepNetworkError: string;
    nameStepCheckError: string;
    nameStepCheckErrorWithDetail: string;
    nameStepApiDisabledError: string;
    nameStepBootstrapMessage: string;
    save: string;
    saving: string;
    saveRequested: string;
    saveHint: string;
    saveCommandMessage: string;
    agentCreatedPendingRefresh: string;
    more: string;
    agentCreated: string;
    startChatting: string;
    backToGallery: string;
  };

  // Breadcrumb
  breadcrumb: {
    workspace: string;
    chats: string;
  };

  // Workspace
  workspace: {
    officialWebsite: string;
    githubTooltip: string;
    settingsAndMore: string;
    visitGithub: string;
    reportIssue: string;
    contactUs: string;
    about: string;
    logout: string;
    adminConsole: string;
    studio: string;
    gatewayUnavailable: string;
    gatewayUnavailableRetrying: string;
  };

  // Conversation
  conversation: {
    noMessages: string;
    startConversation: string;
  };

  // Chats
  chats: {
    searchChats: string;
    loadMoreToSearch: string;
    loadingMore: string;
    loadOlderChats: string;
  };

  // Channels
  channels: {
    title: string;
    connect: string;
    modify: string;
    reconnect: string;
    disconnect: string;
    connected: string;
    notConnected: string;
    pending: string;
    revoked: string;
    disabled: string;
    unconfigured: string;
    unavailable: string;
    unavailableShort: string;
    setupTitle: (name: string) => string;
    setupEditTitle: (name: string) => string;
    setupDescription: string;
    saveAndConnect: string;
    saveChanges: string;
    descriptions: Record<string, string>;
    connectedAs: (name: string) => string;
  };

  // Page titles (document title)
  pages: {
    appName: string;
    chats: string;
    newChat: string;
    untitled: string;
  };

  // Tool calls
  toolCalls: {
    moreSteps: (count: number) => string;
    lessSteps: string;
    executeCommand: string;
    presentFiles: string;
    needYourHelp: string;
    useTool: (toolName: string) => string;
    searchForRelatedInfo: string;
    searchForRelatedImages: string;
    searchFor: (query: string) => string;
    searchForRelatedImagesFor: (query: string) => string;
    searchOnWebFor: (query: string) => string;
    viewWebPage: string;
    listFolder: string;
    readFile: string;
    writeFile: string;
    clickToViewContent: string;
    writeTodos: string;
    skillInstallTooltip: string;
  };

  // Uploads
  uploads: {
    uploading: string;
    uploadingFiles: string;
  };

  // Subtasks
  subtasks: {
    subtask: string;
    executing: (count: number) => string;
    in_progress: string;
    completed: string;
    failed: string;
  };

  // Token Usage
  tokenUsage: {
    title: string;
    label: string;
    input: string;
    output: string;
    total: string;
    view: string;
    unavailable: string;
    unavailableShort: string;
    note: string;
    presets: {
      off: string;
      summary: string;
      perTurn: string;
      debug: string;
    };
    presetDescriptions: {
      off: string;
      summary: string;
      perTurn: string;
      debug: string;
    };
    finalAnswer: string;
    stepTotal: string;
    sharedAttribution: string;
    subagent: (description: string) => string;
    startTodo: (content: string) => string;
    completeTodo: (content: string) => string;
    updateTodo: (content: string) => string;
    removeTodo: (content: string) => string;
  };

  // Shortcuts
  shortcuts: {
    searchActions: string;
    noResults: string;
    actions: string;
    keyboardShortcuts: string;
    keyboardShortcutsDescription: string;
    openCommandPalette: string;
    toggleSidebar: string;
  };

  // Settings
  settings: {
    title: string;
    description: string;
    sections: {
      account: string;
      appearance: string;
      channels: string;
      memory: string;
      tools: string;
      skills: string;
      notification: string;
      about: string;
    };
    memory: {
      title: string;
      description: string;
      empty: string;
      rawJson: string;
      exportButton: string;
      exportSuccess: string;
      importButton: string;
      importConfirmTitle: string;
      importConfirmDescription: string;
      importFileLabel: string;
      importInvalidFile: string;
      importSuccess: string;
      manualFactSource: string;
      addFact: string;
      addFactTitle: string;
      editFactTitle: string;
      addFactSuccess: string;
      editFactSuccess: string;
      clearAll: string;
      clearAllConfirmTitle: string;
      clearAllConfirmDescription: string;
      clearAllSuccess: string;
      factDeleteConfirmTitle: string;
      factDeleteConfirmDescription: string;
      factDeleteSuccess: string;
      factContentLabel: string;
      factCategoryLabel: string;
      factConfidenceLabel: string;
      factContentPlaceholder: string;
      factCategoryPlaceholder: string;
      factConfidenceHint: string;
      factSave: string;
      factValidationContent: string;
      factValidationConfidence: string;
      noFacts: string;
      summaryReadOnly: string;
      memoryFullyEmpty: string;
      factPreviewLabel: string;
      searchPlaceholder: string;
      filterAll: string;
      filterFacts: string;
      filterSummaries: string;
      noMatches: string;
      markdown: {
        overview: string;
        userContext: string;
        work: string;
        personal: string;
        topOfMind: string;
        historyBackground: string;
        recentMonths: string;
        earlierContext: string;
        longTermBackground: string;
        updatedAt: string;
        facts: string;
        empty: string;
        table: {
          category: string;
          confidence: string;
          confidenceLevel: {
            veryHigh: string;
            high: string;
            normal: string;
            unknown: string;
          };
          content: string;
          source: string;
          createdAt: string;
          view: string;
        };
      };
    };
    appearance: {
      themeTitle: string;
      themeDescription: string;
      system: string;
      light: string;
      dark: string;
      systemDescription: string;
      lightDescription: string;
      darkDescription: string;
      languageTitle: string;
      languageDescription: string;
    };
    tools: {
      title: string;
      description: string;
      adminRequired: string;
      empty: string;
    };
    channels: {
      title: string;
      description: string;
      disabled: string;
    };
    skills: {
      title: string;
      description: string;
      createSkill: string;
      emptyTitle: string;
      emptyDescription: string;
      emptyButton: string;
    };
    notification: {
      title: string;
      description: string;
      requestPermission: string;
      deniedHint: string;
      testButton: string;
      testTitle: string;
      testBody: string;
      notSupported: string;
      disableNotification: string;
    };
    account: {
      profileTitle: string;
      email: string;
      role: string;
      changePasswordTitle: string;
      changePasswordDescription: string;
      currentPassword: string;
      newPassword: string;
      confirmNewPassword: string;
      passwordMismatch: string;
      passwordTooShort: string;
      passwordChangedSuccess: string;
      networkError: string;
      updating: string;
      updatePassword: string;
      signOut: string;
    };
    acknowledge: {
      emptyTitle: string;
      emptyDescription: string;
    };
  };

  // Admin Console
  admin: {
    title: string;
    backToWorkspace: string;
    nav: {
      runs: string;
      usage: string;
      audit: string;
    };
    runs: {
      title: string;
      description: string;
      loadError: string;
      unknownError: string;
      loading: string;
      loadMore: string;
      emptyTitle: string;
      emptyDescription: string;
      columns: {
        runId: string;
        status: string;
        model: string;
        tokens: string;
        user: string;
        created: string;
        error: string;
      };
    };
    usage: {
      title: string;
      description: string;
      loadError: string;
      unknownError: string;
      noCompletedRuns: string;
      tooltipFormatter: (tokens: string, runs: number) => string;
      kpi: {
        totalTokens: string;
        totalRuns: string;
        avgTokens: string;
        outputInputRatio: string;
      };
      charts: {
        tokensByModel: string;
        tokensByModelDescription: string;
        tokensByCaller: string;
        tokensByCallerDescription: string;
      };
      breakdown: {
        leadAgent: string;
        subagent: string;
        middleware: string;
      };
    };
    audit: {
      title: string;
      description: string;
      failures24h: string;
      failureRate: string;
      totalRuns24h: string;
      statsUnavailable: string;
      emptyTitle: (status: string) => string;
      emptyDescription: string;
    };
    filter: {
      all: string;
      status: string;
      allStatuses: string;
    };
  };

  // Studio (Agent artifact & release console)
  studio: {
    title: string;
    backToWorkspace: string;
    nav: {
      packages: string;
      import: string;
    };
    packages: {
      title: string;
      description: string;
      importAgent: string;
      loadError: string;
      loadErrorFallback: string;
      emptyTitle: string;
      emptyDescription: string;
      columns: {
        name: string;
        displayName: string;
        status: string;
        created: string;
      };
    };
    detail: {
      loadErrorPackage: string;
      loadErrorVersions: string;
      loadErrorChannels: string;
      tabs: {
        versions: string;
        channels: string;
        overview: string;
      };
      newVersion: string;
      versionEmptyTitle: string;
      versionEmptyDescription: string;
      versionColumns: {
        version: string;
        digest: string;
        status: string;
        size: string;
        created: string;
        actions: string;
      };
      actions: {
        reviewing: string;
        review: string;
        publishing: string;
        publish: string;
        revoking: string;
        revoke: string;
      };
      currentLabel: string;
      emptyPointer: string;
      promoteLabel: string;
      rollbackLabel: string;
      selectVersionPlaceholder: string;
      toLabel: string;
      historyLabel: string;
      byLabel: string;
      systemActor: string;
      promotePermTitle: (perm: string) => string;
      promotePermTitleDev: (perm: string) => string;
      rollbackPermTitle: (perm: string) => string;
      metaTitle: string;
      reconcile: string;
      reconciling: string;
      archive: string;
      archiving: string;
      meta: {
        id: string;
        name: string;
        displayName: string;
        description: string;
        status: string;
        workspace: string;
        createdBy: string;
        createdAt: string;
        updatedAt: string;
      };
    };
    importPage: {
      title: string;
      description: string;
      methodTitle: string;
      methodDescription: string;
      permissionHint: string;
      successImported: string;
      successIdempotent: string;
      successImportedDesc: string;
      successIdempotentDesc: string;
      labels: {
        agentDirName: string;
        agentDirNameTitle: string;
        version: string;
        displayName: string;
        displayNamePlaceholder: string;
        userId: string;
        userIdPlaceholder: string;
        description: string;
        descriptionPlaceholder: string;
      };
      importing: string;
      submit: string;
      meta: {
        package: string;
        version: string;
        status: string;
        digest: string;
      };
    };
    newVersion: {
      title: string;
      back: string;
      permissionHint: string;
      basics: string;
      basicsDescription: string;
      semverError: string;
      contentLabel: string;
      contentPlaceholder: string;
      manifestCore: string;
      manifestCoreDescription: string;
      schemaVersion: string;
      agentEntry: string;
      agentEntryPlaceholder: string;
      soulPrompt: string;
      soulPromptPlaceholder: string;
      modelRequirements: string;
      skills: string;
      skillsDescription: string;
      tools: string;
      mcpServers: string;
      mcpServersDescription: string;
      dependencies: string;
      dependenciesDescription: string;
      networkRequirements: string;
      networkRequirementsDescription: string;
      secretRequirements: string;
      secretRequirementsDescription: string;
      runtimeLimits: string;
      maxSteps: string;
      maxTokens: string;
      timeout: string;
      cancel: string;
      creating: string;
      submit: string;
      addLabels: {
        model: string;
        skill: string;
        tool: string;
        mcp: string;
        dependency: string;
        network: string;
        secret: string;
      };
    };
    dynamicList: {
      removeRow: string;
    };
  };
}
