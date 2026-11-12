import {beforeEach,expect,it,vi} from 'vitest';
import {financeApi} from '../financeAssistant';
const http=vi.hoisted(()=>({get:vi.fn(),post:vi.fn()}));
vi.mock('../index',()=>({default:http}));
beforeEach(()=>vi.resetAllMocks());
it('uses the authenticated same-origin BFF paths and preserves Core conversation IDs',async()=>{
 http.get.mockResolvedValueOnce({data:{managed:true,bound:true}});await expect(financeApi.status()).resolves.toEqual({managed:true,bound:true});expect(http.get).toHaveBeenCalledWith('/api/v1/finance/assistant/status');
 http.get.mockResolvedValueOnce({data:{messages:[],streaming:[]}});await financeApi.messages('same-core-id');expect(http.get).toHaveBeenLastCalledWith('/api/v1/finance/assistant/messages',{params:{conversationId:'same-core-id'}});
 http.post.mockResolvedValue({data:{conversationId:'same-core-id'}});await financeApi.action('input',{conversationId:'same-core-id',text:'hello',clientId:'replay-id'});expect(http.post).toHaveBeenCalledWith('/api/v1/finance/assistant/input',{conversationId:'same-core-id',text:'hello',clientId:'replay-id'});
});
it('supports old deployments missing the BFF but fails closed on auth/network/malformed state',async()=>{
 http.get.mockRejectedValueOnce({response:{status:404}});await expect(financeApi.status()).resolves.toEqual({managed:false,bound:false});
 const exchangeError = {response:{status:404,data:{detail:{error:'FINANCE_CORE_EXCHANGE_FAILED'}}}};
 http.get.mockRejectedValueOnce(exchangeError);await expect(financeApi.status(true)).rejects.toBe(exchangeError);
 http.get.mockRejectedValueOnce({response:{status:401}});await expect(financeApi.status()).rejects.toEqual({response:{status:401}});
 http.get.mockResolvedValueOnce({data:{}});await expect(financeApi.status()).rejects.toThrow('状态响应无效');
});
