import * as vscode from 'vscode';

class AiDevLaunchBridgeProvider
implements vscode.TreeDataProvider<string> {
	getTreeItem(element: string): vscode.TreeItem {
		return new vscode.TreeItem(element);
	}

	getChildren(): vscode.ProviderResult<string[]> {
		return [];
	}
}

export function registerAiDevActionsView(
	context: vscode.ExtensionContext
): void {
	const view = vscode.window.createTreeView(
		'aiDev.launcher',
		{
			treeDataProvider: new AiDevLaunchBridgeProvider(),
			showCollapseAll: false,
			canSelectMany: false,
		}
	);

	let launchInProgress = false;

	const visibilitySubscription =
		view.onDidChangeVisibility(async (event) => {
			if (!event.visible || launchInProgress) {
				return;
			}

			launchInProgress = true;

			try {
				await vscode.commands.executeCommand(
					'aiDev.launchAssistant'
				);

				// Activity Bar entries are view containers, so VS Code
				// briefly opens the Primary Sidebar first. Close it after
				// launching the assistant to make the icon behave like a
				// direct launcher.
				await vscode.commands.executeCommand(
					'workbench.action.toggleSidebarVisibility'
				);
			} finally {
				launchInProgress = false;
			}
		});

	context.subscriptions.push(
		view,
		visibilitySubscription
	);
}
