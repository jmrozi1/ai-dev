import * as vscode from 'vscode';

export interface AssistantChatSessionInfo {
	modelName: string;
}

export interface AssistantChatBackend extends vscode.Disposable {
	startSession(token: vscode.CancellationToken): Promise<AssistantChatSessionInfo>;
	sendMessage(text: string, token: vscode.CancellationToken): Promise<string>;
	sendIsolatedMessage(
		text: string,
		token: vscode.CancellationToken
	): Promise<string>;
}

interface ModelMetadata {
	id: string;
	name: string;
	family: string;
	vendor: string;
}

function getModelMetadata(model: vscode.LanguageModelChat): ModelMetadata {
	const raw = model as unknown as {
		id?: unknown;
		name?: unknown;
		family?: unknown;
		vendor?: unknown;
	};

	return {
		id: typeof raw.id === 'string' ? raw.id : '',
		name: typeof raw.name === 'string' ? raw.name : '',
		family: typeof raw.family === 'string' ? raw.family : '',
		vendor: typeof raw.vendor === 'string' ? raw.vendor : '',
	};
}

export function getAssistantModelDisplayName(
	model: vscode.LanguageModelChat
): string {
	const metadata = getModelMetadata(model);

	return (
		metadata.name.trim()
		|| metadata.family.trim()
		|| metadata.id.trim()
		|| 'Language Model'
	);
}

async function collectResponse(
	model: vscode.LanguageModelChat,
	messages: vscode.LanguageModelChatMessage[],
	token: vscode.CancellationToken
): Promise<string> {
	const response = await model.sendRequest(messages, {}, token);
	let responseText = '';

	for await (const chunk of response.text) {
		if (token.isCancellationRequested) {
			break;
		}

		responseText += chunk;
	}

	return responseText;
}

export class VsCodeAssistantChatBackend implements AssistantChatBackend {
	private preferredModelId = '';
	private modelName = 'Language Model';
	private messages: vscode.LanguageModelChatMessage[] = [];

	async startSession(
		token: vscode.CancellationToken
	): Promise<AssistantChatSessionInfo> {
		const model = await this.selectModel();

		if (token.isCancellationRequested) {
			throw new Error('Assistant startup cancelled.');
		}

		const metadata = getModelMetadata(model);
		this.preferredModelId = metadata.id;
		this.modelName = getAssistantModelDisplayName(model);
		this.messages = [];

		return {
			modelName: this.modelName,
		};
	}

	async sendMessage(
		text: string,
		token: vscode.CancellationToken
	): Promise<string> {
		const userMessage = vscode.LanguageModelChatMessage.User(text);
		const requestMessages = [...this.messages, userMessage];

		let model = await this.selectModel();

		if (token.isCancellationRequested) {
			throw new Error('Assistant request cancelled.');
		}

		let responseText = await collectResponse(
			model,
			requestMessages,
			token
		);

		// A model handle selected during extension startup may be present before
		// its provider is fully ready. Retry once with a newly selected handle.
		if (!responseText.trim() && !token.isCancellationRequested) {
			model = await this.selectModel();
			responseText = await collectResponse(
				model,
				requestMessages,
				token
			);
		}

		if (token.isCancellationRequested) {
			throw new Error('Assistant request cancelled.');
		}

		if (responseText.trim()) {
			this.messages = [
				...requestMessages,
				vscode.LanguageModelChatMessage.Assistant(responseText),
			];
		}

		return responseText;
	}

	async sendIsolatedMessage(
		text: string,
		token: vscode.CancellationToken
	): Promise<string> {
		const requestMessages = [
			vscode.LanguageModelChatMessage.User(text),
		];

		let model = await this.selectModel();

		if (token.isCancellationRequested) {
			throw new Error('Assistant request cancelled.');
		}

		let responseText = await collectResponse(
			model,
			requestMessages,
			token
		);

		if (!responseText.trim() && !token.isCancellationRequested) {
			model = await this.selectModel();
			responseText = await collectResponse(
				model,
				requestMessages,
				token
			);
		}

		if (token.isCancellationRequested) {
			throw new Error('Assistant request cancelled.');
		}

		return responseText;
	}

	dispose(): void {
		this.messages = [];
		this.preferredModelId = '';
		this.modelName = 'Language Model';
	}

	private async selectModel(): Promise<vscode.LanguageModelChat> {
		const models = await vscode.lm.selectChatModels();

		if (models.length === 0) {
			throw new Error(
				'No VS Code language model is available. Make sure a chat model is installed and enabled.'
			);
		}

		if (this.preferredModelId) {
			const preferred = models.find(
				(model) => getModelMetadata(model).id === this.preferredModelId
			);

			if (preferred) {
				return preferred;
			}
		}

		return models[0];
	}
}
