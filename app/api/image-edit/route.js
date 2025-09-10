export const runtime = 'edge';

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export async function OPTIONS() {
  return new Response(null, { 
    status: 204, 
    headers: corsHeaders 
  });
}

export async function POST(request) {
  try {
    const body = await request.json();
    console.log('Received image editing request');
    
    // n8nのWebhookを呼び出し
    const n8nResponse = await fetch(
      'https://norion8789.app.n8n.cloud/webhook/image-edit-api-binary',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          imageBase64: body.imageBase64,
          instructions: body.instructions || '画像の前歯二本を真っ白にホワイトニングしてください。前歯二本以外は元画像のままで変更しないでください。',
          mimeType: body.mimeType || 'image/jpeg'
        })
      }
    );

    if (!n8nResponse.ok) {
      console.error('n8n API error:', n8nResponse.status);
      throw new Error(`n8n API error: ${n8nResponse.status}`);
    }

    // 画像データを取得
    const imageBlob = await n8nResponse.blob();
    console.log('Image processed successfully');
    
    // 画像を返却
    return new Response(imageBlob, {
      status: 200,
      headers: {
        ...corsHeaders,
        'Content-Type': 'image/jpeg',
        'Cache-Control': 'no-cache'
      }
    });

  } catch (error) {
    console.error('Error processing image:', error);
    
    return new Response(
      JSON.stringify({ 
        error: 'Failed to process image',
        details: error.message,
        timestamp: new Date().toISOString()
      }),
      {
        status: 500,
        headers: {
          ...corsHeaders,
          'Content-Type': 'application/json'
        }
      }
    );
  }
}
