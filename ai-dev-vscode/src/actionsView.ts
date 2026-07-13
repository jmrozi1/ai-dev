import * as vscode from 'vscode';
import { AI_DEV_WORKFLOWS, type AiDevWorkflow } from './workflows';

interface WorkflowGroupNode {
	type: 'group';
	id: 'workflows';
	label: string;
}

interface WorkflowNode {
	type: 'workflow';
	workflow: AiDevWorkflow;
	id: string;
	label: string;
}

interface SettingNode {
	type: 'setting';
	id: 'settings';
	label: string;
}

interface LaunchAssistantNode {
	type: 'launchAssistant';
	id: 'launchAssistant';
	label: string;
}

type AiDevWorkflowNode = WorkflowGroupNode | WorkflowNode | SettingNode | LaunchAssistantNode;

const WORKFLOW_GROUP: WorkflowGroupNode = {
	type: 'group',
	id: 'workflows',
	label: 'Workflows',
};

const SETTINGS_NODE: SettingNode = {
	type: 'setting',
	id: 'settings',
	label: 'Settings',
};

const LAUNCH_ASSISTANT_NODE: LaunchAssistantNode = {
	type: 'launchAssistant',
	id: 'launchAssistant',
	label: 'Launch Assistant',
};

export function getAiDevRootNodes(): AiDevWorkflowNode[] {
	return [LAUNCH_ASSISTANT_NODE, WORKFLOW_GROUP, SETTINGS_NODE];
}

class AiDevWorkflowTreeItem extends vscode.TreeItem {
	constructor(node: AiDevWorkflowNode) {
		super(
			node.label,
			node.type === 'group' ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None
		);

		if (node.type === 'group') {
			this.iconPath = new vscode.ThemeIcon('list-tree');
			this.contextValue = 'aiDevWorkflowGroup';
			return;
		}

		if (node.type === 'setting') {
			this.iconPath = new vscode.ThemeIcon('settings-gear');
			this.command = {
				command: 'aiDev.settings',
				title: node.label,
			};
			this.contextValue = 'aiDevSettingItem';
			return;
		}

		if (node.type === 'launchAssistant') {
			this.iconPath = new vscode.ThemeIcon('terminal');
			this.command = {
				command: 'aiDev.launchAssistant',
				title: node.label,
			};
			this.contextValue = 'aiDevLaunchAssistantItem';
			return;
		}

		this.iconPath = new vscode.ThemeIcon('symbol-event');
		this.command = {
			command: 'aiDev.selectWorkflow',
			title: node.label,
			arguments: [node.id],
		};
		this.contextValue = 'aiDevWorkflowItem';
	}
}

export class AiDevWorkflowsProvider implements vscode.TreeDataProvider<AiDevWorkflowNode> {
	private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<AiDevWorkflowNode | undefined>();

	readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;

	getTreeItem(element: AiDevWorkflowNode): vscode.TreeItem {
		return new AiDevWorkflowTreeItem(element);
	}

	getChildren(element?: AiDevWorkflowNode): Thenable<AiDevWorkflowNode[]> {
		if (!element) {
			return Promise.resolve(getAiDevRootNodes());
		}

		if (element.type === 'group' && element.id === 'workflows') {
			const children: WorkflowNode[] = AI_DEV_WORKFLOWS.map((workflow) => ({
				type: 'workflow',
				workflow,
				id: workflow.id,
				label: workflow.label,
			}));
			return Promise.resolve(children);
		}

		return Promise.resolve([]);
	}

	refresh(): void {
		this.onDidChangeTreeDataEmitter.fire(undefined);
	}
}

export function registerAiDevActionsView(context: vscode.ExtensionContext): void {
	const provider = new AiDevWorkflowsProvider();
	context.subscriptions.push(vscode.window.registerTreeDataProvider('aiDev.workflows', provider));
}