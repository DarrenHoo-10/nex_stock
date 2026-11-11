import apiClient from './index';
export type Source = {
    kind?: string;
    id?: string;
    code?: string;
    name?: string;
    tradeDate?: string;
};
export type Message = {
    id: string;
    role: string;
    content: string;
    seq: number;
    input_status?: string;
    source_key?: string;
};
export type Topic = {
    conversationId: string;
    title: string;
    audience: string;
    sourceContext: Record<string, unknown>;
    last_activity_at: number;
    message_count: number;
    unread_count: number;
    messages: Message[];
    activeTask: {
        id: string;
        status: string;
    } | null;
};
export type Models = {
    selection?: {
        selected_connection: string;
        selected_model: string;
        selected_reasoning: string | null;
        model_selection_version: number;
    };
    connections: Array<{
        id: string;
        name: string;
        defaultModel: string;
        enabled: boolean;
        configured: boolean;
        models: string[] | null;
        names?: Record<string, string>;
        options: Record<string, {
            reasoning: string[];
        }>;
    }>;
};
export type Feed = {
    sync?: { privacyEpoch: number; bootId: string };
    hasMore?: boolean;
    messages: Message[];
    streaming: Array<{
        taskId: string;
        text: string;
    }>;
};
const base = '/api/v1/finance/assistant/';
export const financeApi = {
  async status() {
    try {
      const {data}=await apiClient.get<{managed:boolean;bound:boolean}>(base+'status');
      if(typeof data.managed!=='boolean'||typeof data.bound!=='boolean')throw new Error('金融助手状态响应无效');
      return data;
    } catch(error) {
      if(error&&typeof error==='object'&&'response' in error&&(error.response as {status?:number})?.status===404)return {managed:false,bound:false};
      throw error;
    }
  },
  connect:async()=>(await apiClient.post<{managed:boolean;bound:boolean}>(base+'connect',{})).data,
  topics:async()=>(await apiClient.get<{topics:Topic[]}>(base+'topics')).data.topics,
  messages:async(conversationId:string, cursor?: {after: number} | {before: number})=>(await apiClient.get<Feed>(base+'messages',{params:{conversationId,...cursor}})).data,
  models:async()=>(await apiClient.get<Models>(base+'models')).data,
  action:async<T=unknown>(action:string,data:Record<string,unknown>)=>(await apiClient.post<T>(base+action,data)).data,
};
